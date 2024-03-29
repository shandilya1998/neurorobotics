"""
Mujoco Maze environment.
Based on `models`_ and `rllab`_.

.. _models: https://github.com/tensorflow/models/tree/master/research/efficient-hrl
.. _rllab: https://github.com/rll/rllab
"""

"""
    REFER TO THE FOLLOWING FOR GEOM AND BODY NAMES IN MODEL:
        https://github.com/openai/mujoco-py/blob/9dd6d3e8263ba42bfd9499a988b36abc6b8954e9/mujoco_py/generated/wrappers.pxi
"""

import itertools as it
import os
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, List, Optional, Tuple, Type
import gym
import numpy as np
import networkx as nx
from neurorobotics.simulations import maze_env_utils, maze_task
from neurorobotics.simulations.agent_model import AgentModel
from neurorobotics.utils.env_utils import convert_observation_to_space, \
    calc_spline_course, TargetCourse, proportional_control, \
    State, pure_pursuit_steer_control
import random
import copy
from neurorobotics.constants import params
import math
import cv2
import tf_agents as tfa
import copy
# Directory that contains mujoco xml files.
MODEL_DIR = os.path.join(os.getcwd(), 'assets', 'xml')

class MazeEnv(tfa.environments.py_environment.PyEnvironment):
    def __init__(
        self,
        model_cls: Type[AgentModel],
        maze_task: Type[maze_task.MazeTask] = maze_task.MazeTask,
        max_episode_size: int = 2000,
        n_steps = 5,
        include_position: bool = True,
        maze_height: float = 0.5,
        maze_size_scaling: float = 4.0,
        inner_reward_scaling: float = 1.0,
        restitution_coef: float = 0.8,
        task_kwargs: dict = {},
        websock_port: Optional[int] = None,
        camera_move_x: Optional[float] = None,
        camera_move_y: Optional[float] = None,
        camera_zoom: Optional[float] = None,
        image_shape: Tuple[int, int] = (600, 480),
        **kwargs,
    ) -> None:

        # Attribute Initialisation
        self.collision_count = 0
        self.n_steps = n_steps
        self.kwargs = kwargs
        self.top_view_size = params['top_view_size']
        self.t = 0  # time steps
        self.total_steps = 0 
        self.ep = 0
        self.max_episode_size = max_episode_size
        self._task = maze_task(maze_size_scaling, **task_kwargs)
        self._maze_height = height = maze_height
        self._maze_size_scaling = size_scaling = maze_size_scaling
        self._inner_reward_scaling = inner_reward_scaling
        self._put_spin_near_agent = self._task.PUT_SPIN_NEAR_AGENT
        # Observe other objectives
        self._restitution_coef = restitution_coef

        self._maze_structure = structure = self._task.create_maze()
        # Elevate the maze to allow for falling.
        self.elevated = any(maze_env_utils.MazeCell.CHASM in row for row in structure)
        # Are there any movable blocks?
        self.blocks = any(any(r.can_move() for r in row) for row in structure)
        torso_x, torso_y = self._find_robot()
        self._init_torso_x = torso_x
        self._init_torso_y = torso_y
        self._init_positions = [
            (x - torso_x, y - torso_y) for x, y in self._find_all_robots()
        ]

        # Utility Methods
        def func(x):
            x_int, x_frac = int(x), x % 1
            if x_frac > 0.5:
                x_int += 1
            return x_int

        def func2(x):
            x_int, x_frac = int(x), x % 1 
            if x_frac > 0.5:
                x_int += 1
                x_frac -= 0.5
            else:
                x_frac += 0.5
            return x_int, x_frac

        self._xy_to_rowcol = lambda x, y: (
            func((y + torso_y) / size_scaling),
            func((x + torso_x) / size_scaling),
        )
        self._xy_to_rowcol_v2 = lambda x, y: (
            func2((y + torso_y) / size_scaling),
            func2((x + torso_x) / size_scaling), 
        )
        self._rowcol_to_xy = lambda r, c: (
            c * size_scaling - torso_x,
            r * size_scaling - torso_y
        )

        self._structure_to_graph_index = lambda \
            row, col: row * len(self._maze_structure[0]) + col
        
        # Let's create MuJoCo XML
        self.model_cls = model_cls
        self._websock_port = websock_port
        self._camera_move_x = camera_move_x
        self._camera_move_y = camera_move_y
        self._camera_zoom = camera_zoom
        self._image_shape = image_shape
        self._mj_offscreen_viewer = None
        self._websock_server_pipe = None
        self.set_env()
        super(MazeEnv, self).__init__()

    def set_env(self):
        # Environment Building 
        # Modifications of `xml` file
        xml_path = os.path.join(MODEL_DIR, self.model_cls.FILE)
        tree = ET.parse(xml_path)
        worldbody = tree.find(".//worldbody")

        height_offset = 0.0
        if self.elevated:
            # Increase initial z-pos of ant.
            height_offset = self._maze_height * self._maze_size_scaling
            torso = tree.find(".//body[@name='torso']")
            torso.set("pos", f"0 0 {0.75 + height_offset:.2f}")
        if self.blocks:
            # If there are movable blocks, change simulation settings to perform
            # better contact detection.
            default = tree.find(".//default")
            default.find(".//geom").set("solimp", ".995 .995 .01")

        tree.find('.//option').set('timestep', str(params['dt']))
        self.movable_blocks = []
        self.object_balls = []
        torso_x, torso_y = self._find_robot()
        self.obstacles = []
        for i in range(len(self._maze_structure)):
            for j in range(len(self._maze_structure[0])):
                struct = self._maze_structure[i][j]
                if struct.is_robot() and self._put_spin_near_agent:
                    struct = maze_env_utils.MazeCell.SPIN
                x, y = j * self._maze_size_scaling - torso_x, i * self._maze_size_scaling - torso_y
                h = self._maze_height / 2 * self._maze_size_scaling
                size = self._maze_size_scaling * 0.5
                if self.elevated and not struct.is_chasm():
                    # Create elevated platform.
                    ET.SubElement(
                        worldbody,
                        "geom",
                        name=f"elevated_{i}_{j}",
                        pos=f"{x} {y} {h}",
                        size=f"{size} {size} {h}",
                        type="box",
                        material="MatObj",
                        contype="1",
                        conaffinity="1",
                        rgba="0.9 0.9 0.9 1"
                    )
                    self.obstacles.append(self._xy_to_rowcol(x, y))
                if struct.is_block():
                    # Unmovable block.
                    # Offset all coordinates so that robot starts at the origin.
                    ET.SubElement(
                        worldbody,
                        "geom",
                        name=f"block_{i}_{j}",
                        pos=f"{x} {y} {h + height_offset}",
                        size=f"{size} {size} {h}",
                        type="box",
                        material="MatObj",
                        contype="1",
                        conaffinity="1",
                        rgba="0.4 0.4 0.4 1"
                    )
                    self.obstacles.append(self._xy_to_rowcol(x, y))
                elif struct.can_move():
                    # Movable block.
                    self.movable_blocks.append(f"movable_{i}_{j}")
                    _add_movable_block(
                        worldbody,
                        struct,
                        i,
                        j,
                        self._maze_size_scaling,
                        x,
                        y,
                        h,
                        height_offset,
                    )
                    self.obstacles.append(self._xy_to_rowcol(x, y))
                elif struct.is_object_ball():
                    # Movable Ball
                    self.object_balls.append(f"objball_{i}_{j}")
                    _add_object_ball(worldbody, i, j, x, y, self._task.OBJECT_BALL_SIZE)
                    self.obstacles.append(self._xy_to_rowcol(x, y))

        torso = tree.find(".//body[@name='torso']")
        geoms = torso.findall(".//geom")
        for geom in geoms:
            if "name" not in geom.attrib:
                raise Exception("Every geom of the torso must have a name")

        # Set goals
        for i, goal in enumerate(self._task.goals):
            z = goal.pos[2] if goal.dim >= 3 else 0.1 *  self._maze_size_scaling
            if goal.custom_size is None:
                size = f"{self._maze_size_scaling * 0.1}"
            else:
                size = f"{goal.custom_size}"
            ET.SubElement(
                worldbody,
                "site",
                name=f"goal_site{i}",
                pos=f"{goal.pos[0]} {goal.pos[1]} {z}",
                size=f"{self._maze_size_scaling * 0.1}",
                rgba=goal.rgb.rgba_str(),
                material = "MatObj"
            )
        
        # MuJoCo Model config
        self._episode_ended = False
        _, file_path = tempfile.mkstemp(text=True, suffix=".xml")
        tree.write(file_path)
        self.world_tree = tree
        self.wrapped_env = self.model_cls(file_path=file_path, **self.kwargs)
        self.dt = self.wrapped_env.dt
        assert self.dt == params['dt']
        self.model = self.wrapped_env.model
        self.data = self.wrapped_env.data
        self.obstacles_ids = []
        self.agent_ids = []
        for name in self.model.geom_names:
            if 'block' in name:
                self.obstacles_ids.append(self.model._geom_name2id[name])
            elif 'obj' in name:
                self.obstacles_ids.append(self.model._geom_name2id[name])
            elif name != 'floor':
                self.agent_ids.append(self.model._geom_name2id[name])

        # Environment Utility Methods
        self.get_xy = lambda: self.wrapped_env.get_xy()
        self.get_ori = lambda: self.wrapped_env.get_ori()
        self._normalize_action = lambda action, low, high: 2 * (action - low) / (low - high) - 1

        # Action and Observation Space Config
        self._set_action_space()
        self._reward_spec = tfa.specs.ArraySpec(
            shape = (),
            dtype = np.float32
        )
        self.last_wrapped_obs = self.wrapped_env.reset().copy()
        action = self.action_space.sample()
        self.actions = [np.zeros_like(action) for i in range(self.n_steps)]
        goal = self._task.goals[self._task.goal_index].pos - self.get_xy()
        goal = np.array([
            2 * np.linalg.norm(goal) / np.linalg.norm(self._task.goals[self._task.goal_index].pos) - 1,
            self.check_angle(np.arctan2(goal[1], goal[0]) - self.get_ori()) / np.pi
        ], dtype = np.float32)
        self.goals = [goal.copy() for i in range(self.n_steps)]

        # Supplementary Planning Algorithm Config
        self.__create_maze_graph()
        self.sampled_path = self.__sample_path()
        self._current_cell = copy.deepcopy(self.sampled_path[0])
        self.__find_all_waypoints()
        self.__find_cubic_spline_path()
        self.__setup_vel_control()
        inertia = np.concatenate([
            self.data.qvel,
            self.data.qacc
        ], -1)
        ob = self._get_obs()
        self._set_observation_space(ob)

    def _set_action_space(self):
        self._action_space = self.wrapped_env.action_space
        self._action_spec = tfa.specs.BoundedArraySpec(
            shape = self._action_space.shape,
            dtype = self._action_space.dtype,
            minimum = self._action_space.low,
            maximum = self._action_space.high
        )

    def __create_maze_graph(self):
        num_row = len(self._maze_structure)
        num_col = len(self._maze_structure[0])
        num_vertices = num_row * num_col
        # Using Networkx for easy access to graph methods
        self._maze_graph = nx.DiGraph()
        self._maze_graph.add_nodes_from(np.arange(
            0, num_vertices
        ))

        # Graph Node and Edge Initialisation
        for i in range(num_row):
            for j in range(num_col):
                self._maze_graph.nodes[
                    self._structure_to_graph_index(i, j)
                ]['struct'] = self._maze_structure[i][j]
                self._maze_graph.nodes[
                    self._structure_to_graph_index(i, j)
                ]['row'] = i
                self._maze_graph.nodes[
                    self._structure_to_graph_index(i, j)
                ]['col'] = j
                self._maze_graph.nodes[
                    self._structure_to_graph_index(i, j)
                ]['index'] = self._structure_to_graph_index(i, j)

        for i in range(num_row):
            for j in range(num_col):
                self.__add_edges_to_maze_graph(self._maze_graph.nodes[
                    self._structure_to_graph_index(i, j)
                ])

    def __add_edges_to_maze_graph(self, node):
        # Considering all the blocks surrounding a node
        neighbors = [
            (node['row'] - 1, node['col']),
            (node['row'], node['col'] - 1),
            (node['row'] + 1, node['col']),
            (node['row'], node['col'] + 1),
            (node['row'] + 1, node['col'] + 1),
            (node['row'] + 1, node['col'] - 1),
            (node['row'] - 1, node['col'] + 1),
            (node['row'] - 1, node['col'] - 1)
        ]
        for neighbor in neighbors:
            if self.__check_structure_index_validity(
                neighbor[0],
                neighbor[1]
            ):
                # Movement Allowed only along the open blocks
                if not self._maze_graph.nodes[
                    self._structure_to_graph_index(
                        neighbor[0],
                        neighbor[1]
                    )
                ]['struct'].is_wall_or_chasm():
                    self._maze_graph.add_edge(
                        node['index'],
                        self._maze_graph.nodes[self._structure_to_graph_index(
                            neighbor[0],
                            neighbor[1]
                        )]['index']
                    )

    def __check_structure_index_validity(self, i, j):
        valid = [True, True]
        if i < 0:
            valid[0] = False
        elif i >= len(self._maze_structure):
            valid[0] = False
        if j < 0:
            valid[1] = False
        elif j >= len(self._maze_structure[0]):
            valid[1] = False
        return valid[0] and valid[1]

    def __sample_path(self):
        # Randomly Sample Possible Path
        robot_x, robot_y = self.get_xy()
        robot_ori = self.get_ori()
        row, col = self._xy_to_rowcol(robot_x, robot_y)
        source = self._structure_to_graph_index(row, col)
        goal_pos = self._task.goals[self._task.goal_index].pos[:2]
        row, col = self._xy_to_rowcol(goal_pos[0], goal_pos[1])
        target = self._structure_to_graph_index(row, col)
        paths = list(nx.algorithms.shortest_paths.generic.all_shortest_paths(
            self._maze_graph,
            source,
            target
        ))
        return random.choice(paths)

    def __find_all_waypoints(self):
        """
            Returns coordinates of the centers of the blocks
            on a sampled path
        """
        self.wx = []
        self.wy = [] 
        for cell in self.sampled_path:
            row, col = self._graph_to_structure_index(cell)
            x, y = self._rowcol_to_xy(row, col) 
            self.wx.append(copy.deepcopy(x))
            self.wy.append(copy.deepcopy(y))
        self.wx.append(self._task.goals[self._task.goal_index].pos[0])
        self.wy.append(self._task.goals[self._task.goal_index].pos[1])
        self.final = [self.wx[-1], self.wy[-1]]

    def _graph_to_structure_index(self, index):
        row = int(index / len(self._maze_structure))
        col = index % len(self._maze_structure[0])
        return row, col

    def __find_cubic_spline_path(self):
        # Cubic Spline interpolation of all the waypoints
        self.cx, self.cy, self.cyaw, self.ck, self.s = calc_spline_course(self.wx, self.wy, params['ds'])
    
    def __setup_vel_control(self):
        # Setup target speed and state container
        if isinstance(params['target_speed'], float):
            self.target_speed = 8.0 
        elif params['target_speed'] == 'random':
            self.target_speed = np.random.choice(np.arange(self.wrapped_env.VELOCITY_LIMITS) * (1.4 - 0.75) + 0.75)
        else:
            raise ValueError('Unexpected Target Speed config')
        self.state = State(
            x = self.wrapped_env.sim.data.qpos[0],
            y = self.wrapped_env.sim.data.qpos[1],
            yaw = self.wrapped_env.sim.data.qpos[2],
            v = np.linalg.norm(self.wrapped_env.sim.data.qvel[:2]),
            WB = 0.2 * self._maze_size_scaling,
        )
        self.last_idx = len(self.cx) - 1
        self.target_course = TargetCourse(self.cx, self.cy)
        self.target_ind, _ = self.target_course.search_target_index(self.state)

    @property
    def action_space(self):
        return self._action_space

    def get_action(self):
        # Sample appropriate action to remain on sampled path
        ai = proportional_control(self.target_speed, self.state.v)
        di, self.target_ind = pure_pursuit_steer_control(
            self.state, self.target_course, self.target_ind
        )
        #yaw = self.state.yaw +  self.state.v / self.state.WB * math.tan(di) * self.dt
        v = self.state.v + ai * self.dt
        vyaw = self.state.v / self.state.WB * math.tan(di)
        #self.state.update(ai, di, self.dt)
        #v = self.state.v
        #yaw = self.state.yaw
        # Refer to simulations/point PointEnv: def step() for more information
        yaw = self.check_angle(self.state.yaw + vyaw * self.dt)
        vx = v * np.cos(yaw)
        vy = v * np.sin(yaw)
        self.sampled_action = np.array([
            v,
            vyaw,
        ], dtype = np.float32)
        return self.sampled_action

    def check_angle(self, angle):
        if angle > np.pi:
            angle -= 2 * np.pi
        elif angle < -np.pi:
            angle += 2 * np.pi
        return angle

    def _xy_limits(self) -> Tuple[float, float, float, float]:
        # Need to check Usage of method
        xmin, ymin, xmax, ymax = 100, 100, -100, -100
        structure = self._maze_structure
        for i, j in it.product(range(len(structure)), range(len(structure[0]))):
            if structure[i][j].is_block():
                continue
            xmin, xmax = min(xmin, j), max(xmax, j)
            ymin, ymax = min(ymin, i), max(ymax, i)
        x0, y0 = self._init_torso_x, self._init_torso_y
        scaling = self._maze_size_scaling
        xmin, xmax = (xmin - 0.5) * scaling - x0, (xmax + 0.5) * scaling - x0
        ymin, ymax = (ymin - 0.5) * scaling - y0, (ymax + 0.5) * scaling - y0
        return xmin, xmax, ymin, ymax

    def _get_obs(self):
        obs = self.last_wrapped_obs
        img = obs['front'].copy()
        img = img.astype(np.float32) / 255
        img = np.concatenate([
            img.astype(np.float32) / 255.0, np.expand_dims(obs['front_depth'], -1)
        ], -1)
        # Observations are normalised in the environment
        high = self.action_space.high
        low = self.action_space.low
        sampled_action = self.get_action().astype(np.float32)
        actions = [self._normalize_action(action, low, high) for action in self.actions]
        goals = self.goals
        max_vel = np.array([
            self.wrapped_env.VELOCITY_LIMITS,
            self.wrapped_env.VELOCITY_LIMITS,
            params['max_vyaw']
        ])
        min_vel = -max_vel
        sensors = np.concatenate([
            2 * (self.data.qvel.copy() - min_vel) / (max_vel - min_vel) - 1,
            np.array([self.get_ori() / np.pi], dtype = np.float32),
        ] + actions + goals, -1).astype(np.float32)

        if params['debug']:
            cv2.imshow('stream front', cv2.cvtColor(obs['front'], cv2.COLOR_RGB2BGR))
            #cv2.imshow('stream back', cv2.cvtColor(obs['back'], cv2.COLOR_RGB2BGR))
            #cv2.imshow('stream left', cv2.cvtColor(obs['left'], cv2.COLOR_RGB2BGR))
            #cv2.imshow('stream right', cv2.cvtColor(obs['right'], cv2.COLOR_RGB2BGR))
            cv2.imshow('depth stream', obs['front_depth'])
            cv2.imshow('mask', reversemask)
            top = self.render('rgb_array')
            cv2.imshow('position stream', top)
            #cv2.imshow('mask', mask)
            #cv2.imshow('gray', gray)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                pass
        
        """
            obs = [
                `visual`,
                `sensors`,
            ]
        """
        obs = (
            img.copy(),
            sensors.copy(),
        )
        return obs

    def _set_observation_space(self, observation):
        self.observation_space = gym.spaces.Tuple([
            gym.spaces.Box(
                low = np.zeros_like(observation[0]),
                high = np.ones_like(observation[0]),
                shape = observation[0].shape,
                dtype = observation[0].dtype
            ),
            gym.spaces.Box(
                low = -np.ones_like(observation[1]),
                high = np.ones_like(observation[1]),
                shape = observation[1].shape,
                dtype = observation[1].dtype
            ),
        ])

        self._observation_spec = (
            tfa.specs.BoundedArraySpec(
                shape = self.observation_space[0].shape,
                dtype = self.observation_space[0].dtype,
                minimum = self.observation_space[0].low,
                maximum = self.observation_space[0].high
            ),
            tfa.specs.BoundedArraySpec(
                shape = self.observation_space[1].shape,
                dtype = self.observation_space[1].dtype,
                minimum = self.observation_space[1].low,
                maximum = self.observation_space[1].high
            ),
        )
        return self.observation_space

    def _reset(self) -> np.ndarray:
        # Need to modify this method
        self.collision_count = 0
        self.t = 0
        self._task.set()
        #self.set_env()
        for i, goal in enumerate(self._task.goals):
            self.model.site_pos[i, :2] = goal.pos
            rgb = np.array([
                goal.rgb.red,
                goal.rgb.green,
                goal.rgb.blue
            ], dtype = np.float32)
            self.model.site_rgba[i, :3] = rgb
        self.wrapped_env.reset()
        # Samples a new start position
        if len(self._init_positions) > 1:
            xy = np.random.choice(self._init_positions)
            self.wrapped_env.set_xy(xy)
        action = self.actions[0]
        self.actions = [np.zeros_like(action) for i in range(self.n_steps)]
        goal = self._task.goals[self._task.goal_index].pos - self.get_xy()
        goal = np.array([
            2 * np.linalg.norm(goal) / np.linalg.norm(self._task.goals[self._task.goal_index].pos) - 1,
            self.check_angle(np.arctan2(goal[1], goal[0]) - self.get_ori()) / np.pi
        ], dtype = np.float32)
        self.goals = [goal.copy() for i in range(self.n_steps)]
        self.sampled_path = self.__sample_path()
        self._current_cell = copy.deepcopy(self.sampled_path[0])
        self.__find_all_waypoints()
        self.__find_cubic_spline_path()
        self.__setup_vel_control()
        obs = self._get_obs()
        self._episode_ended = False
        return tfa.trajectories.time_step.restart(
            observation = obs,
            reward_spec = self._reward_spec
        )

    def _step(self, action: np.ndarray) -> Tuple[np.ndarray, float, bool, dict]:
        if self._episode_ended:
            # The last action ended the episode. Ignore the current action and start
            # a new episode.
            return self.reset()
        # Perform MuJuCo Simulation
        action = np.clip(action, a_min = self.action_space.low, a_max = self.action_space.high)
        self.t += 1
        self.actions.pop(0)
        self.actions.append(action.copy())
        self.last_wrapped_obs, inner_reward, _, info = self.wrapped_env.step(action)
        inframe, reversemask = self._task.goals[self._task.goal_index].inframe(self.last_wrapped_obs['front'])
        next_pos = self.get_xy()
        x, y = next_pos
        (row, row_frac), (col, col_frac) = self._xy_to_rowcol_v2(next_pos[0], next_pos[1])
        yaw = self.get_ori()
        v = np.linalg.norm(self.data.qvel[:2])
        self.state.set(x, y, v, yaw)
        collision_penalty = 0.0
        next_obs = self._get_obs()

        # Computing the reward in "https://ieeexplore.ieee.org/document/8398461"
        goal = self._task.goals[self._task.goal_index].pos - next_pos
        theta_t = self.check_angle(np.arctan2(goal[1], goal[0]) - yaw)
        rho = np.linalg.norm(goal) / np.linalg.norm(self._task.goals[self._task.goal_index].pos)
        goal = np.array([
            2 * rho - 1,
            self.check_angle(np.arctan2(goal[1], goal[0]) - self.get_ori()) / np.pi
        ], dtype = np.float32)
        rho = rho * 0.1
        self.goals.pop(0)
        self.goals.append(goal)
        qvel = self.wrapped_env.data.qvel.copy()
        vyaw = qvel[self.wrapped_env.ORI_IND]
        vmax = self.wrapped_env.VELOCITY_LIMITS * 1.4

        # Reward Computation
        inner_reward += -1 + (v / vmax) * np.cos(theta_t) * (1 - (np.abs(vyaw) / params['max_vyaw']))
        outer_reward = self._task.reward(next_pos, inframe) + rho
        done = self._task.termination(next_pos, inframe)
        index = self.__get_current_cell(next_pos)
        self._current_cell = index
        almost_collision, blind, outbound = self.check_position(next_pos)
        if almost_collision:
            collision_penalty += -1.0 * self._inner_reward_scaling
        next_obs, penalty = self.conditional_blind(next_obs, yaw, blind)
        collision_penalty += penalty
        if done:
            outer_reward += 200.0
        if outbound:
            collision_penalty += -10.0 * self._inner_reward_scaling
            next_obs[0] = np.zeros_like(next_obs[0])
            done = True
        if self.t > self.max_episode_size:
            done = True
        reward = np.array(
            inner_reward + outer_reward + collision_penalty, 
            dtype = np.float32
        )
        if done:
            self._episode_ended = True
            return tfa.trajectories.time_step.termination(
                observation = next_obs,
                reward = reward,
            )
        return tfa.trajectories.time_step.transition(
            observation = next_obs,
            reward = reward,
            discount = params['gamma'],
        )

    def __get_current_cell(self, next_pos):
        robot_x, robot_y = next_pos
        row, col = self._xy_to_rowcol(robot_x, robot_y)
        index = self._structure_to_graph_index(row, col) 
        return index

    def conditional_blind(self, obs, yaw, b):
        # Blinds the visual observation if agent is too close to the wall or collides
        penalty = 0.0
        obs = list(obs)
        if b[0]:
            if 0 > yaw >= -np.pi / 2:
                if 0 > yaw >= -np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                elif -np.pi / 8 > yaw > -3 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            elif -np.pi / 2 > yaw >= -np.pi:
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi / 2 > yaw >= 0:
                if np.pi / 8 > yaw >= 0:
                    obs[0] = np.zeros_like(obs[0])
                elif 3 * np.pi / 8 >= yaw > np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi > yaw >= np.pi / 2:
                penalty += -1.0 * self._inner_reward_scaling
            else:
                raise ValueError
        if b[1]:
            if 0 > yaw >= -np.pi / 2:
                penalty += -1.0 * self._inner_reward_scaling
            elif -np.pi / 2 > yaw >= -np.pi:
                if -np.pi / 2 > yaw >= -5 * np.pi / 8:
                    pass
                elif -5 * np.pi / 8 > yaw >= -7 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                else:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi / 2 > yaw >= 0:
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi > yaw >= np.pi / 2:
                if 5 * np.pi / 8 > yaw >= np.pi / 2:
                    pass
                elif 7 * np.pi / 8 > yaw >= 5 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                else:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            else:
                raise ValueError
        if b[2]:
            if 0 > yaw >= -np.pi / 2:
                penalty += -1.0 * self._inner_reward_scaling
            elif -np.pi / 2 > yaw >= -np.pi:
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi / 2 > yaw >= 0:
                if np.pi / 8 > yaw >= 0:
                    pass
                elif 3 * np.pi / 8 >= yaw > np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                else:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi > yaw >= np.pi / 2:
                if 5 * np.pi / 8 > yaw >= np.pi / 2:
                    obs[0] = np.zeros_like(obs[0])
                elif 7 * np.pi / 8 > yaw >= 5 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            else:
                raise ValueError
        if b[3]:
            if 0 > yaw >= -np.pi / 2:
                if 0 > yaw >= -np.pi / 8:
                    pass
                elif -np.pi / 8 > yaw > -3 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                else:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            elif -np.pi / 2 > yaw >= -np.pi:
                if -np.pi / 2 > yaw >= -5 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                elif -5 * np.pi / 8 > yaw >= -7 * np.pi / 8:
                    obs[0] = np.zeros_like(obs[0])
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi / 2 > yaw >= 0:
                penalty += -1.0 * self._inner_reward_scaling
            elif np.pi > yaw >= np.pi / 2:
                penalty += -1.0 * self._inner_reward_scaling
            else:
                raise ValueError
        if self._is_in_collision():
            penalty += -50.0 * self._inner_reward_scaling
            self.collision_count += 1
        return tuple(obs), penalty

    def _is_in_collision(self):
        # Checks if the last action caused collision
        for contact in self.data.contact:
            geom1 = contact.geom1
            geom2 = contact.geom2
            if geom1 != 0 and geom2 != 0:
                if geom1 in self.obstacles_ids:
                    if geom2 in self.agent_ids:
                        return True
                if geom2 in self.obstacles_ids:
                    if geom1 in self.agent_ids:
                        return True
            else:
                return False


    def check_position(self, pos):
        # Checks position of the agent relative to the walls
        # Checks if agent is within bounds or not
        (row, row_frac), (col, col_frac) = self._xy_to_rowcol_v2(pos[0], pos[1])
        blind = [False, False, False, False]
        collision = False
        outbound = False
        neighbors = [
            (row, col + 1),
            (row, col - 1),
            (row + 1, col),
            (row - 1, col),
        ]
        order = ['front', 'back', 'left', 'right']
        row_frac -= 0.5
        col_frac -= 0.5
        rpos = np.array([row_frac, col_frac], dtype = np.float32)
        if row > 0 and col > 0 and row < len(self._maze_structure) - 1 and col < len(self._maze_structure[0]) - 1:
            for i, (nrow, ncol) in enumerate(neighbors):
                if not self._maze_structure[nrow][ncol].is_empty():
                    rdir = (nrow - row)
                    cdir = (ncol - col)
                    direction = np.array([rdir, cdir], dtype = np.float32)
                    distance = np.dot(rpos, direction)
                    if distance > 0.325:
                        collision = True
                    if distance > 0.35:
                        blind[i] = True
        else:
            outbound = True
        #print('almost collision: {}, blind: {}'.format(collision, blind))
        return collision, blind, outbound

    def get_top_view(self):
        block_size = self.top_view_size

        img = np.zeros(
            (int(block_size * len(self._maze_structure)), int(block_size * len(self._maze_structure[0])), 3),
            dtype = np.uint8
        )

        for i in range(len(self._maze_structure)):
            for j in range(len(self._maze_structure[0])):
                if  self._maze_structure[i][j].is_wall_or_chasm():
                    img[
                        int(block_size * i): int(block_size * (i + 1)),
                        int(block_size * j): int(block_size * (j + 1))
                    ] = 128


        def xy_to_imgrowcol(x, y):
            (row, row_frac), (col, col_frac) = self._xy_to_rowcol_v2(x, y)
            row = block_size * row + int((row_frac) * block_size)
            col = block_size * col + int((col_frac) * block_size)
            return int(row), int(col)

        pos = self.wrapped_env.get_xy()
        row, col = xy_to_imgrowcol(pos[0], pos[1])
        img[row - int(block_size / 10): row + int(block_size / 10), col - int(block_size / 10): col + int(block_size / 10)] = [255, 255, 255]
        for i, goal in enumerate(self._task.goals):
            pos = goal.pos
            row, col = xy_to_imgrowcol(pos[0], pos[1])
            if i == self._task.goal_index:
                img[
                    row - int(block_size / 10): row + int(block_size / 10),
                    col - int(block_size / 10): col + int(block_size / 10)
                ] = [0, 0, 255]
            else:
                img[
                    row - int(block_size / 10): row + int(block_size / 10),
                    col - int(block_size / 10): col + int(block_size / 10)
                ] = [0, 255, 0]

        return np.flipud(img)

    def xy_to_imgrowcol(self, x, y):
        (row, row_frac), (col, col_frac) = self._xy_to_rowcol_v2(x, y)
        row = self.top_view_size * row + int(row_frac * self.top_view_size)
        col = self.top_view_size * col + int(col_frac * self.top_view_size)
        return row, col

    def render(self, mode = 'human', **kwargs):
        if mode == 'rgb_array':
            return self.get_top_view()
        elif mode == "human" and self._websock_port is not None:
            if self._mj_offscreen_viewer is None:
                from mujoco_py import MjRenderContextOffscreen as MjRCO
                from mujoco_maze.websock_viewer import start_server

                self._mj_offscreen_viewer = MjRCO(self.wrapped_env.sim)
                self._maybe_move_camera(self._mj_offscreen_viewer)
                self._websock_server_pipe = start_server(self._websock_port)
            return self._websock_server_pipe.send(self._render_image())
        else:
            if self.wrapped_env.viewer is None:
                self.wrapped_env.render(mode, **kwargs)
                self._maybe_move_camera(self.wrapped_env.viewer)
            return self.wrapped_env.render(mode, **kwargs)

    @property
    def viewer(self) -> Any:
        if self._websock_port is not None:
            return self._mj_viewer
        else:
            return self.wrapped_env.viewer

    def _render_image(self) -> np.ndarray:
        self._mj_offscreen_viewer._set_mujoco_buffers()
        self._mj_offscreen_viewer.render(640, 480)
        return np.asarray(
            self._mj_offscreen_viewer.read_pixels(640, 480, depth=False)[::-1, :, :],
            dtype=np.uint8,
        )

    def _maybe_move_camera(self, viewer: Any) -> None:
        from mujoco_py import const

        if self._camera_move_x is not None:
            viewer.move_camera(const.MOUSE_ROTATE_V, self._camera_move_x, 0.0)
        if self._camera_move_y is not None:
            viewer.move_camera(const.MOUSE_ROTATE_H, 0.0, self._camera_move_y)
        if self._camera_zoom is not None:
            viewer.move_camera(const.MOUSE_ZOOM, 0, self._camera_zoom)

    def _find_robot(self) -> Tuple[float, float]:
        structure = self._maze_structure
        size_scaling = self._maze_size_scaling
        for i, j in it.product(range(len(structure)), range(len(structure[0]))):
            if structure[i][j].is_robot():
                return j * size_scaling, i * size_scaling
        raise ValueError("No robot in maze specification.")

    def _find_all_robots(self) -> List[Tuple[float, float]]:
        structure = self._maze_structure
        size_scaling = self._maze_size_scaling
        coords = []
        for i, j in it.product(range(len(structure)), range(len(structure[0]))):
            if structure[i][j].is_robot():
                coords.append((j * size_scaling, i * size_scaling))
        return coords

    def _objball_positions(self) -> None:
        return [
            self.wrapped_env.get_body_com(name)[:2].copy() for name in self.object_balls
        ]

    def close(self) -> None:
        self.wrapped_env.close()
        if self._websock_server_pipe is not None:
            self._websock_server_pipe.send(None)

    def action_spec(self):
        return self._action_spec

    def observation_spec(self):
        return self._observation_spec

def _add_object_ball(
    worldbody: ET.Element, i: str, j: str, x: float, y: float, size: float
) -> None:
    body = ET.SubElement(worldbody, "body", name=f"objball_{i}_{j}", pos=f"{x} {y} 0")
    mass = 0.0001 * (size ** 3)
    ET.SubElement(
        body,
        "geom",
        type="sphere",
        name=f"objball_{i}_{j}_geom",
        size=f"{size}",  # Radius
        pos=f"0.0 0.0 {size}",  # Z = size so that this ball can move!!
        rgba=maze_task.BLUE.rgba_str(),
        contype="1",
        conaffinity="1",
        solimp="0.9 0.99 0.001",
        mass=f"{mass}",
    )
    ET.SubElement(
        body,
        "joint",
        name=f"objball_{i}_{j}_x",
        axis="1 0 0",
        pos="0 0 0.0",
        type="slide",
    )
    ET.SubElement(
        body,
        "joint",
        name=f"objball_{i}_{j}_y",
        axis="0 1 0",
        pos="0 0 0",
        type="slide",
    )
    ET.SubElement(
        body,
        "joint",
        name=f"objball_{i}_{j}_rot",
        axis="0 0 1",
        pos="0 0 0",
        type="hinge",
        limited="false",
    )

def _add_movable_block(
    worldbody: ET.Element,
    struct: maze_env_utils.MazeCell,
    i: str,
    j: str,
    size_scaling: float,
    x: float,
    y: float,
    h: float,
    height_offset: float,
) -> None:
    falling = struct.can_move_z()
    if struct.can_spin():
        h *= 0.1
        x += size_scaling * 0.25
        shrink = 0.1
    elif falling:
        # The "falling" blocks are shrunk slightly and increased in mass to
        # ensure it can fall easily through a gap in the platform blocks.
        shrink = 0.99
    elif struct.is_half_block():
        shrink = 0.5
    else:
        shrink = 1.0
    size = size_scaling * 0.5 * shrink
    movable_body = ET.SubElement(
        worldbody,
        "body",
        name=f"movable_{i}_{j}",
        pos=f"{x} {y} {h}",
    )
    ET.SubElement(
        movable_body,
        "geom",
        name=f"block_{i}_{j}",
        pos="0 0 0",
        size=f"{size} {size} {h}",
        type="box",
        material="",
        mass="0.001" if falling else "0.0002",
        contype="1",
        conaffinity="1",
        rgba="0.9 0.1 0.1 1",
    )
    if struct.can_move_x():
        ET.SubElement(
            movable_body,
            "joint",
            axis="1 0 0",
            name=f"movable_x_{i}_{j}",
            armature="0",
            damping="0.0",
            limited="true" if falling else "false",
            range=f"{-size_scaling} {size_scaling}",
            margin="0.01",
            pos="0 0 0",
            type="slide",
        )
    if struct.can_move_y():
        ET.SubElement(
            movable_body,
            "joint",
            armature="0",
            axis="0 1 0",
            damping="0.0",
            limited="true" if falling else "false",
            range=f"{-size_scaling} {size_scaling}",
            margin="0.01",
            name=f"movable_y_{i}_{j}",
            pos="0 0 0",
            type="slide",
        )
    if struct.can_move_z():
        ET.SubElement(
            movable_body,
            "joint",
            armature="0",
            axis="0 0 1",
            damping="0.0",
            limited="true",
            range=f"{-height_offset} 0",
            margin="0.01",
            name=f"movable_z_{i}_{j}",
            pos="0 0 0",
            type="slide",
        )
    if struct.can_spin():
        ET.SubElement(
            movable_body,
            "joint",
            armature="0",
            axis="0 0 1",
            damping="0.0",
            limited="false",
            name=f"spinable_{i}_{j}",
            pos="0 0 0",
            type="ball",
        )

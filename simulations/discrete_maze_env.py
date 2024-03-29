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

# Directory that contains mujoco xml files.
MODEL_DIR = os.path.join(os.getcwd(), 'assets', 'xml')

class MazeEnv(gym.Env):
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

    def set_env(self):
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
                    self.obstacles.append(f"elevated_{i}_{j}")
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
                        rgba="0.4 0.4 0.4 1".format(
                            np.random.random(),
                            np.random.random()
                        ),
                    )
                    self.obstacles.append(f"block_{i}_{j}")
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
                    self.obstacles.append(f"movable_{i}_{j}")
                elif struct.is_object_ball():
                    # Movable Ball
                    self.object_balls.append(f"objball_{i}_{j}")
                    _add_object_ball(worldbody, i, j, x, y, self._task.OBJECT_BALL_SIZE)
                    self.obstacles.append(f"objball_{i}_{j}")

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
        self._set_action_space()
        self.last_wrapped_obs = self.wrapped_env._get_obs().copy()
        action = self.action_space.sample()
        self.actions = [np.zeros_like(action) for i in range(self.n_steps)]
        goal = self._task.goals[self._task.goal_index].pos - self.wrapped_env.get_xy()
        self.goals = [goal.copy() for i in range(self.n_steps)]
        inertia = np.concatenate([
            self.data.qvel,
            self.data.qacc
        ], -1)
        ob = self._get_obs()
        self._set_observation_space(ob)

    @property
    def action_space(self):
        return self._action_space

    def get_ori(self) -> float:
        return self.wrapped_env.get_ori()

    def _set_action_space(self):
        self._action_space = self.wrapped_env.action_space

    def _set_observation_space(self, observation):
        spaces = {
            'window' : gym.spaces.Box(
                low = np.zeros_like(observation['window'], dtype = np.uint8),
                high = 255 * np.ones_like(observation['window'], dtype = np.uint8),
                shape = observation['window'].shape,
                dtype = observation['window'].dtype
            ),   
            'frame_t' : gym.spaces.Box(
                low = np.zeros_like(observation['frame_t'], dtype = np.uint8),
                high = 255 * np.ones_like(observation['frame_t'], dtype = np.uint8),
                shape = observation['frame_t'].shape,
                dtype = observation['frame_t'].dtype
            ),   
            'scale_3' : gym.spaces.Box(
                low = np.zeros_like(observation['scale_3'], dtype = np.uint8),
                high = 255 * np.ones_like(observation['scale_3'], dtype = np.uint8),
                shape = observation['scale_3'].shape,
                dtype = observation['scale_3'].dtype
            ),   
            'sensors' : gym.spaces.Box(
                low = -np.ones_like(observation['sensors']),
                high = np.ones_like(observation['sensors']),
                shape = observation['sensors'].shape,
                dtype = observation['sensors'].dtype
            ),   
            'sampled_action' : copy.deepcopy(self._action_space),
            'inframe' : gym.spaces.Box(
                low = np.zeros_like(observation['inframe']),
                high = np.ones_like(observation['inframe']),
                shape = observation['inframe'].shape,
                dtype = observation['inframe'].dtype
            ),
            'depth' : gym.spaces.Box(
                low = np.zeros_like(observation['depth'], dtype = np.float32),
                high = np.ones_like(observation['depth'], dtype = np.float32),
                shape = observation['depth'].shape,
                dtype = observation['depth'].dtype
            ),
        }
    
        if params['add_ref_scales']:
            spaces['ref_window'] = gym.spaces.Box(
                low = np.zeros_like(observation['ref_window'], dtype = np.uint8),
                high = 255 * np.ones_like(observation['ref_window'], dtype = np.uint8),
                shape = observation['ref_window'].shape,
                dtype = observation['ref_window'].dtype
            )
            spaces['ref_frame_t'] = gym.spaces.Box(
                low = np.zeros_like(observation['ref_frame_t'], dtype = np.uint8),
                high = 255 * np.ones_like(observation['ref_frame_t'], dtype = np.uint8),
                shape = observation['ref_frame_t'].shape,
                dtype = observation['ref_frame_t'].dtype
            )

        self.observation_space = gym.spaces.Dict(spaces)

        return self.observation_space

    def _xy_limits(self) -> Tuple[float, float, float, float]:
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

    def check_angle(self, angle):
        if angle > np.pi:
            angle -= 2 * np.pi
        elif angle < -np.pi:
            angle += 2 * np.pi
        return angle

    def __get_scale_indices(self, x, y, w, h, scale, size):
        center_x, center_y = x + w // 2, y + h // 2 
        x_min = center_x - size // (scale * 2) 
        x_max = center_x + size // (scale * 2) 
        y_min = center_y - size // (scale * 2) 
        y_max = center_y + size // (scale * 2) 
        if x_min < 0: 
            center_x += np.abs(x_min)
            x_max += np.abs(x_min)
            x_min = 0
        if x_max > size:
            offset = x_max - size
            center_x -= offset
            x_min -= offset
        if y_min < 0: 
            center_y += np.abs(y_min)
            y_max += np.abs(y_min)
            y_min = 0
        if y_max > size:
            offset = y_max - size
            center_y -= offset
            y_min -= offset
        return x_min, x_max, y_min, y_max

    def get_scales(self, frame, bbx):
        size = frame.shape[0]
        assert frame.shape[0] == frame.shape[1]
        window = frame.copy()
        frame_t = frame.copy()
        x, y, w, h = None, None, None, None

        if len(bbx) > 0:
            x, y, w, h = bbx
        else:
            # Need to keep eye sight in the upper part of the image
            x = size // 2 - size // 4
            y = size // 2 - size // 4
            w = size // 2
            h = size // 2
        
        #print(x + w // 2, y + h // 2)
        
        # scale 1
        scale = 5
        x_min, x_max, y_min, y_max = self.__get_scale_indices(
            x, y, w, h, scale, size
        )

        window = window[y_min:y_max, x_min:x_max]

        # scale 2
        scale = 2
        x_min, x_max, y_min, y_max = self.__get_scale_indices(
            x, y, w, h, scale, size
        )
        frame_t = frame_t[y_min:y_max, x_min:x_max]

        return window, frame_t

    def detect_target(self, frame):
        
        """
            Refer to the following link for reference to openCV code:
            https://answers.opencv.org/question/229620/drawing-a-rectangle-around-the-red-color-region/
        """

        hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
        low, high = maze_task.get_hsv_ranges(maze_task.RED)
        low = np.array(low, dtype = np.uint8)
        high = np.array(high, dtype = np.uint8)
        mask = cv2.inRange (hsv, low, high)
        contours, _ =  cv2.findContours(mask.copy(),
                           cv2.RETR_TREE,
                           cv2.CHAIN_APPROX_SIMPLE)
        bbx = []
        if len(contours):
            red_area = max(contours, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(red_area)
            cv2.rectangle(frame,(x, y),(x+w, y+h),(0, 0, 255), 1)
            bbx.extend([x, y, w, h]) 
        return bbx 

    def _get_obs(self) -> np.ndarray:
        obs = self.wrapped_env._get_obs()
        img = obs['front'].copy()
        assert img.shape[0] == img.shape[1]

        # Target Detection and Attention Window
        bbx = self.detect_target(img)
        window, frame_t = self.get_scales(obs['front'], bbx)
        inframe = True if len(bbx) > 0 else False 

        # Sampled Action
        high = self.action_space.high
        sampled_action = self.get_action().astype(np.float32)

        # Sensor Readings
        ## Goal
        goal = self._task.goals[self._task.goal_index].pos - self.wrapped_env.get_xy()
        goal = np.array([
            np.linalg.norm(goal) / np.linalg.norm(self._task.goals[self._task.goal_index].pos),
            self.check_angle(np.arctan2(goal[1], goal[0]) - self.get_ori()) / np.pi
        ], dtype = np.float32)
        ## Velocity
        max_vel = np.array([
            self.wrapped_env.VELOCITY_LIMITS,
            self.wrapped_env.VELOCITY_LIMITS,
            self.action_space.high[1]
        ])
        min_vel = -max_vel
        sensors = np.concatenate([
            self.data.qvel.copy() / max_vel,
            (self.actions[-1].copy() - self.action_space.low) / (self.action_space.high - self.action_space.low),
            np.array([self.get_ori() / np.pi, self.t / self.max_episode_size], dtype = np.float32),
            goal.copy(),
        ], -1)


        if params['debug']:
            size = img.shape[0]
            cv2.imshow('stream front', cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
            cv2.imshow('stream scale 1', cv2.cvtColor(cv2.resize(
                window, (size, size)
            ), cv2.COLOR_RGB2BGR))
            cv2.imshow('stream scale 2', cv2.cvtColor(cv2.resize(
                frame_t, (size, size)
            ), cv2.COLOR_RGB2BGR))
            cv2.imshow('depth stream', obs['front_depth'])
            top = self.render('rgb_array')
            cv2.imshow('position stream', top)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                pass

        shape = window.shape[:2]
        frame_t = cv2.resize(frame_t, shape)
        scale_3 = cv2.resize(obs['front'], shape)
        depth = np.expand_dims(
            cv2.resize(
                obs['front_depth'].copy(), shape
            ), 0
        )

        _obs = {
            'window' : window.copy(),
            'frame_t' : frame_t.copy(),
            'scale_3' : scale_3.copy(),
            'sensors' : sensors,
            'sampled_action' : sampled_action.copy(),
            'depth' : depth,
            'inframe' : np.array([inframe], dtype = np.float32),
        }

        if params['add_ref_scales']:
            ref_window, ref_frame_t = self.get_scales(obs['front'].copy(), []) 
            ref_frame_t = cv2.resize(ref_frame_t, shape)
            _obs['ref_window'] = ref_window.copy()
            _obs['ref_frame_t'] = ref_frame_t.copy()

        return _obs

    def reset(self) -> np.ndarray:
        self.collision_count = 0
        self.t = 0
        self.close()
        self._task.set()
        self.set_env()
        self.wrapped_env.reset()
        
        ori = np.random.uniform(low = -3 * np.pi / 5, high = np.pi / 10)
        self.wrapped_env.set_ori(ori)

        # Samples a new start position
        if len(self._init_positions) > 1:
            xy = np.random.choice(self._init_positions)
            self.wrapped_env.set_xy(xy)
        action = self.actions[0]
        self.actions = [np.zeros_like(action) for i in range(self.n_steps)]
        goal = self._task.goals[self._task.goal_index].pos - self.wrapped_env.get_xy()
        self.goals = [goal.copy() for i in range(self.n_steps)]
        obs = self._get_obs()
        return obs

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

    def _render_image(self) -> np.ndarray:
        self._mj_offscreen_viewer._set_mujoco_buffers()
        self._mj_offscreen_viewer.render(*self._image_shape)
        pixels = self._mj_offscreen_viewer.read_pixels(*self._image_shape, depth=False)
        return np.asarray(pixels[::-1, :, :], dtype=np.uint8)

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

    def _is_in_collision(self):
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

    def conditional_blind(self, obs, yaw, b):
        penalty = 0.0
        collisions = np.zeros((12,), dtype = np.float32)
        for direction in b:
            if direction:
                penalty += -1.0 * self._inner_reward_scaling
        
        if self._is_in_collision():
            #print('collision')
            penalty += -50.0 * self._inner_reward_scaling
            self.collision_count += 1
            obs['window'] = np.zeros_like(obs['window'])
            obs['frame_t'] = np.zeros_like(obs['frame_t'])
            obs['scale_3'] = np.zeros_like(obs['scale_3'])
            obs['depth'] = np.zeros_like(obs['depth'])
        
        return obs, penalty

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, float, bool, dict]:
        # Proprocessing and Environment Update
        action = np.clip(action, a_min = self.action_space.low, a_max = self.action_space.high)
        self.t += 1
        info = {}
        self.actions.pop(0)
        self.actions.append(action.copy())
        inner_next_obs, inner_reward, _, info = self.wrapped_env.step(action)

        # Observation and Parameter Gathering
        x, y = self.wrapped_env.get_xy()
        (row, row_frac), (col, col_frac) = self._xy_to_rowcol_v2(x, y)
        yaw = self.get_ori()
        v = np.linalg.norm(self.data.qvel[:2])
        self.state.set(x, y, v, yaw)
        next_pos = self.wrapped_env.get_xy()
        collision_penalty = 0.0
        next_obs = self._get_obs()

        # Computing the reward in "https://ieeexplore.ieee.org/document/8398461"
        goal = self._task.goals[self._task.goal_index].pos - self.wrapped_env.get_xy()
        rho = (1 - np.linalg.norm(goal) / np.linalg.norm(self._task.goals[self._task.goal_index].pos)) * 0.1
        self.goals.pop(0)
        self.goals.append(goal)
        theta_t = self.check_angle(np.arctan2(goal[1], goal[0]) - self.get_ori())
        qvel = self.wrapped_env.data.qvel.copy()
        vyaw = qvel[self.wrapped_env.ORI_IND]
        vmax = self.wrapped_env.VELOCITY_LIMITS * 1.4
        inner_reward = -1 + (v / vmax) * np.cos(theta_t) * (1 - (np.abs(vyaw) / params['max_vyaw']))
        inner_reward = self._inner_reward_scaling * inner_reward
        #print(rho * 15)

        # Task Reward Computation
        outer_reward = self._task.reward(next_pos, bool(next_obs['inframe'][0])) + rho
        done = self._task.termination(self.wrapped_env.get_xy(),  bool(next_obs['inframe'][0]))
        info["position"] = self.wrapped_env.get_xy()

        # Collision Penalty Computation
        index = self.__get_current_cell()
        self._current_cell = index
        almost_collision, blind, outbound = self.check_position(next_pos)
        if almost_collision:
            collision_penalty += -1.0 * self._inner_reward_scaling
        next_obs, penalty = self.conditional_blind(next_obs, yaw, blind)
        collision_penalty += penalty

        # Reward and Info Declaration
        if done:
            outer_reward += 200.0
            info['is_success'] = True
        else:
            info['is_success'] = False
        if outbound:
            collision_penalty += -10.0 * self._inner_reward_scaling
            next_obs['window'] = np.zeros_like(obs['window'])
            next_obs['frame_t'] = np.zeros_like(obs['frame_t'])
            next_obs['scale_3'] = np.zeros_like(obs['scale_3'])
            done = True
        if self.t > self.max_episode_size:
            done = True
        
        if collision_penalty < -10.0:
            done = True

        reward = inner_reward + outer_reward + collision_penalty
        info['inner_reward'] = inner_reward
        info['outer_reward'] = outer_reward
        info['collision_penalty'] = collision_penalty
        #print('step {} reward: {}'.format(self.t, reward))
        return next_obs, reward, done, info

    def __get_current_cell(self):
        robot_x, robot_y = self.wrapped_env.get_xy()
        row, col = self._xy_to_rowcol(robot_x, robot_y)
        index = self._structure_to_graph_index(row, col)
        return index

    def close(self) -> None:
        self.wrapped_env.close()
        if self._websock_server_pipe is not None:
            self._websock_server_pipe.send(None)

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

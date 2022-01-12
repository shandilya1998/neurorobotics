
params = {
    'input_size_low_level_control': 6,
    'track_list'                  : [ 
                                        'joint_pos',
                                        'action',
                                        'velocity',
                                        'position',
                                        'true_joint_pos',
                                        'sensordata',
                                        'qpos',
                                        'qvel',
                                        'achieved_goal',
                                        'observation',
                                        'heading_ctrl',
                                        'omega',
                                        'z',
                                        'mu',
                                        'd1',
                                        'd2',
                                        'd3',
                                        'stability',
                                        'omega_o',
                                        'reward',
                                        'rewards'
                                    ],
    'max_simulation_time'         : 5.0,
    'min_simulation_time'         : 1.0,
    'show_animation'              : False,
    'dt'                          : 0.02,
    'learning_starts'             : 16008,
    'imitation_steps'             : 1e5,
    'render_freq'                 : 8004,
    'save_freq'                   : 16008,
    'eval_freq'                   : 8004,
    'buffer_size'                 : int(2e5),
    'total_timesteps'             : int(1.1e6),
    'ds'                          : 0.01,
    'motor_cortex'                : [256, 128],
    'snc'                         : [256, 1],
    'af'                          : [256, 1],
    'critic_net_arch'             : [400, 300],
    'OU_MEAN'                     : 0.00,
    'OU_SIGMA'                    : 0.15,
    'top_view_size'               : 50.,
    'batch_size'                  : 50,
    'lr'                          : 1e-3,
    'final_lr'                    : 1e-5,
    'n_steps'                     : 2000,
    'gamma'                       : 0.99,
    'tau'                         : 0.001, 
    'n_updates'                   : 64,
    'num_ctx'                     : 1000,
    'actor_lr'                    : 1e-3,
    'critic_lr'                   : 1e-2,
    'weight_decay'                : 1e-2,
    'collision_threshold'         : 20,
    'debug'                       : False,
}

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
    'learning_starts'             : 100,
    'render_freq'                 : 20000,
    'save_freq'                   : 10000,
    'eval_freq'                   : 10000,
    'buffer_size'                 : 12500,
    'total_timesteps'             : int(1e6),
    'ds'                          : 0.01,
    'motor_cortex'                : [[256, 192], [128, 2]],
    'snc'                         : [256, 1],
    'af'                          : [256, 1],
    'critic_net_arch'             : [400, 300],
    'OU_MEAN'                     : 0.0,
    'OU_SIGMA'                    : 0.08,
    'top_view_size'               : 50.,
    'batch_size'                  : 64,
    'lr'                          : 5e-4,
    'n_steps'                     : 2500,
    'gamma'                       : 0.99,
    'gae_lambda'                  : 0.95,
    'n_updates'                   : 64,
}

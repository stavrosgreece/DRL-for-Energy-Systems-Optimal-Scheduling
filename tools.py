import torch
import pandas as pd
import numpy.random as rd
import os
import numpy as np
from pyomo.core.base.config import default_pyomo_config
from pyomo.core.base.piecewise import Bound
from pyomo.environ import *
from pyomo.opt import SolverFactory
import gurobipy as gp
from gurobipy import GRB
from gurobipy import *
import argparse
from decision_transformer.models.decision_transformer import DecisionTransformer


def optimization_base_result(env, month, day, initial_soc):

    pv = env.data_manager.get_series_pv_data(month, day)
    price = env.data_manager.get_series_price_data(month, day)
    load = env.data_manager.get_series_electricity_cons_data(month, day)
    period = env.episode_length
# parameters
    DG_parameters = env.dg_parameters

    def get_dg_info(parameters):
        p_max = []
        p_min = []
        ramping_up = []
        ramping_down = []
        a_para = []
        b_para = []
        c_para = []

        for name, gen_info in parameters.items():
            p_max.append(gen_info['power_output_max'])
            p_min.append(gen_info['power_output_min'])
            ramping_up.append(gen_info['ramping_up'])
            ramping_down.append(gen_info['ramping_down'])
            a_para.append(gen_info['a'])
            b_para.append(gen_info['b'])
            c_para.append(gen_info['c'])
        return p_max, p_min, ramping_up, ramping_down, a_para, b_para, c_para
    p_max, p_min, ramping_up, ramping_down, a_para, b_para, c_para = get_dg_info(
        parameters=DG_parameters)
    battery_parameters = env.battery_parameters
    NUM_GEN = len(DG_parameters.keys())
    battery_capacity = env.battery.capacity
    battery_efficiency = env.battery.efficiency

    m = gp.Model("UC")
    m.Params.LogToConsole = 0

    # set variables in the system
    on_off = m.addVars(NUM_GEN, period, vtype=GRB.BINARY, name='on_off')
    gen_output = m.addVars(
        NUM_GEN, period, vtype=GRB.CONTINUOUS, name='output')
    battery_energy_change = m.addVars(period, vtype=GRB.CONTINUOUS, lb=-env.battery.max_charge,
                                      ub=env.battery.max_charge, name='battery_action')  # directly set constrains for charge/discharge
    # set constrains for exchange between external grid and distributed energy system
    grid_energy_import = m.addVars(
        period, vtype=GRB.CONTINUOUS, lb=0, ub=env.grid.exchange_ability, name='import')
    grid_energy_export = m.addVars(
        period, vtype=GRB.CONTINUOUS, lb=0, ub=env.grid.exchange_ability, name='export')
    soc = m.addVars(period, vtype=GRB.CONTINUOUS, lb=0.2, ub=0.8, name='SOC')

    # 1. add balance constrain
    m.addConstrs(((sum(gen_output[g, t] for g in range(NUM_GEN))+pv[t]+grid_energy_import[t] >= load[t] +
                 battery_energy_change[t]+grid_energy_export[t]) for t in range(period)), name='powerbalance')
    # 2. add constrain for p max pmin
    m.addConstrs((gen_output[g, t] <= on_off[g, t]*p_max[g]
                 for g in range(NUM_GEN) for t in range(period)), 'output_max')
    m.addConstrs((gen_output[g, t] >= on_off[g, t]*p_min[g]
                 for g in range(NUM_GEN) for t in range(period)), 'output_min')
    # 3. add constrain for ramping up ramping down
    m.addConstrs((gen_output[g, t+1]-gen_output[g, t] <= ramping_up[g]
                 for g in range(NUM_GEN) for t in range(period-1)), 'ramping_up')
    m.addConstrs((gen_output[g, t]-gen_output[g, t+1] <= ramping_down[g]
                 for g in range(NUM_GEN) for t in range(period-1)), 'ramping_down')
    # 4. add constrains for SOC
    m.addConstr(battery_capacity*soc[0] == battery_capacity*initial_soc+(
        battery_energy_change[0]*battery_efficiency), name='soc0')
    m.addConstrs((battery_capacity*soc[t] == battery_capacity*soc[t-1]+(
        battery_energy_change[t]*battery_efficiency)for t in range(1, period)), name='soc update')

    # set cost function
    # 1 cost of generator
    cost_gen = gp.quicksum((a_para[g]*gen_output[g, t]*gen_output[g, t]+b_para[g] *
                           gen_output[g, t]+c_para[g]*on_off[g, t])for t in range(period) for g in range(NUM_GEN))
    cost_grid_import = gp.quicksum(
        grid_energy_import[t]*price[t] for t in range(period))
    cost_grid_export = gp.quicksum(
        grid_energy_export[t]*price[t]*env.sell_coefficient for t in range(period))

    m.setObjective((cost_gen+cost_grid_import-cost_grid_export), GRB.MINIMIZE)
    m.optimize()
    output_record = {'pv': [], 'price': [], 'load': [], 'netload': [], 'soc': [], 'battery_energy_change': [
    ], 'grid_import': [], 'grid_export': [], 'gen1': [], 'gen2': [], 'gen3': [], 'step_cost': []}
    for t in range(period):
        gen_cost = sum((on_off[g, t].x*(a_para[g]*gen_output[g, t].x*gen_output[g,
                       t].x+b_para[g]*gen_output[g, t].x+c_para[g])) for g in range(NUM_GEN))
        grid_import_cost = grid_energy_import[t].x*price[t]
        grid_export_cost = grid_energy_export[t].x * \
            price[t]*env.sell_coefficient
        output_record['pv'].append(pv[t])
        output_record['price'].append(price[t])
        output_record['load'].append(load[t])
        output_record['netload'].append(load[t]-pv[t])
        output_record['soc'].append(soc[t].x)
        output_record['battery_energy_change'].append(
            battery_energy_change[t].x)
        output_record['grid_import'].append(grid_energy_import[t].x)
        output_record['grid_export'].append(grid_energy_export[t].x)
        output_record['gen1'].append(gen_output[0, t].x)
        output_record['gen2'].append(gen_output[1, t].x)
        output_record['gen3'].append(gen_output[2, t].x)
        output_record['step_cost'].append(
            gen_cost+grid_import_cost-grid_export_cost)

    output_record_df = pd.DataFrame.from_dict(output_record)
    return output_record_df


class Arguments:
    '''revise here for our own purpose'''

    def __init__(self, agent=None, env=None):
        self.agent = agent  # Deep Reinforcement Learning algorithm
        self.env = env  # the environment for training
        self.plot_shadow_on = False  # control do we need to plot all shadow figures
        self.cwd = None  # current work directory. None means set automatically
        # remove the cwd folder? (True, False, None:ask me)
        self.if_remove = False
        # self.replace_train_data=True
        # for example: os.environ['CUDA_VISIBLE_DEVICES'] = '0, 2,'
        self.visible_gpu = '0,1,2,3'
        # rollout workers number pre GPU (adjust it to get high GPU usage)
        self.worker_num = 2
        # cpu_num for evaluate model, torch.set_num_threads(self.num_threads)
        self.num_threads = 8

        '''Arguments for training'''
        self.num_episode = 2000
        self.gamma = 0.995  # discount factor of future rewards
        # self.reward_scale = 1  # an approximate target reward usually be closed to 256
        self.learning_rate = 2 ** -14  # 2 ** -14 ~= 6e-5
        self.soft_update_tau = 2 ** -8  # 2 ** -8 ~= 5e-3

        self.net_dim = 256  # the network width 256
        # num of transitions sampled from replay buffer.
        self.batch_size = 4096
        self.repeat_times = 2 ** 5  # repeatedly update network to keep critic's loss small
        self.target_step = 24  # collect target_step experiences , then update network, 1024
        self.max_memo = 500000  # capacity of replay buffer
        # PER for off-policy sparse reward: Prioritized Experience Replay.
        self.if_per_or_gae = False

        '''Arguments for evaluate'''
        # self.eval_gap = 2 ** 6  # evaluate the agent per eval_gap seconds
        # self.eval_times = 2  # number of times that get episode return in first
        self.random_seed = 0  # initialize random seed in self.init_before_training()
        # self.random_seed_list = [1234, 2234, 3234, 4234, 5234]
        self.random_seed_list = [5234]
        '''Arguments for save and plot issues'''
        self.train = True
        self.save_network = True
        self.test_network = True
        self.save_test_data = True
        self.compare_with_pyomo = True
        self.plot_on = True

    def init_before_training(self, if_main):
        if self.cwd is None:
            agent_name = self.agent.__class__.__name__
            self.cwd = f'./{agent_name}'

        if if_main:
            import shutil  # remove history according to bool(if_remove)
            if self.if_remove is None:
                self.if_remove = bool(
                    input(f"| PRESS 'y' to REMOVE: {self.cwd}? ") == 'y')
            elif self.if_remove:
                shutil.rmtree(self.cwd, ignore_errors=True)
                print(f"| Remove cwd: {self.cwd}")
            os.makedirs(self.cwd, exist_ok=True)

        # np.random.seed(self.random_seed)
        # torch.manual_seed(self.random_seed)
        torch.set_num_threads(self.num_threads)
        torch.set_default_dtype(torch.float32)

        os.environ['CUDA_VISIBLE_DEVICES'] = str(
            self.visible_gpu)  # control how many GPU is used 　


def test_one_episode(env, act, device):
    '''to get evaluate information, here record the unblance of after taking action'''
    record_state = []
    record_action = []
    record_reward = []
    record_output = []
    record_cost = []
    record_unbalance = []
    # [time price, netload,action,real action, output*4,soc,unbalance(exchange+penalty)]
    record_system_info = []

    record_init_info = []  # should include month,day,time,intial soc,initial

    env.TRAIN = False
    state = env.reset()
    record_init_info.append(
        [env.month, env.day, env.current_time, env.battery.current_capacity])
    print(
        f'current testing month is {env.month}, day is {env.day},initial_soc is {env.battery.current_capacity}')
    for i in range(24):
        s_tensor = torch.as_tensor((state,), device=device)

        a_tensor = act(s_tensor)

        # not need detach(), because with torch.no_grad() outside
        action = a_tensor.detach().cpu().numpy()[0]
        real_action = action
        state, next_state, reward, done = env.step(action)

        record_system_info.append([state[0], state[1], state[3], action, real_action, env.battery.SOC(
        ), env.battery.energy_change, next_state[4], next_state[5], next_state[6], env.unbalance, env.operation_cost])

        record_state.append(state)
        record_action.append(real_action)
        record_reward.append(reward)
        record_output.append(env.current_output)
        record_unbalance.append(env.unbalance)
        state = next_state
    record_system_info[-1][7:10] = [env.final_step_outputs[0],
                                    env.final_step_outputs[1], env.final_step_outputs[2]]
    # add information of last step soc
    record_system_info[-1][5] = env.final_step_outputs[3]
    record = {'init_info': record_init_info, 'information': record_system_info, 'state': record_state, 'action': record_action,
              'reward': record_reward, 'cost': record_cost, 'unbalance': record_unbalance, 'record_output': record_output}
    return record


def test_one_episode_DT(env, device, model_init=None, month=None, day=None, initial_soc=None, simple_model=False, state_mean=None,
                        state_std=None):
    '''to get evaluate information, here record the unblance of after taking action'''
    record_state = []
    record_action = []
    record_reward = []
    record_output = []
    record_cost = []
    record_unbalance = []
    # [time price, netload,action,real action, output*4,soc,unbalance(exchange+penalty)]
    record_system_info = []
    record_init_info = []  # should include month,day,time,intial soc,initial
    state_dim = 9
    act_dim = 4

    if model_init == None:
        print("Loading model...")
        model = torch.load("model_ratio.pt", map_location=torch.device(device))
    else:
        model = model_init

    # we keep all the histories on the device
    # note that the latest action and reward will be "padding"

    actions = torch.zeros((0, act_dim), device=device, dtype=torch.float32)
    rewards = torch.zeros(0, device=device, dtype=torch.float32)

    target_return = 20000
    ep_return = target_return
    target_return = torch.tensor(
        ep_return, device=device, dtype=torch.float32).reshape(1, 1)
    timesteps = torch.tensor(0, device=device, dtype=torch.long).reshape(1, 1)

    env.TRAIN = False

    if month != None:
        state = env.reset(month=month, day=day, initial_soc=initial_soc)
    else:
        state = env.reset()

    states = torch.from_numpy(state).reshape(
        1, state_dim).to(device=device, dtype=torch.float32)

    record_init_info.append(
        [env.month, env.day, env.current_time, env.battery.current_capacity])
    # print(
    # f'current testing month is {env.month}, day is {env.day},initial_soc is {env.battery.current_capacity}')
    for i in range(24):
        # print(f'current time is {i}')

        cur_state = torch.as_tensor(state).to(
            device=device).reshape(1, state_dim)
        if i != 0:
            states = torch.cat([states, cur_state], dim=0)
            rewards[-1] = torch.as_tensor(reward).to(device) + \
                torch.sum(rewards)

        # add padding
        actions = torch.cat([actions, torch.zeros(
            (1, act_dim), device=device)], dim=0)
        rewards = torch.cat([rewards, torch.zeros(1, device=device)])
        # print(actions,rewards,states,target_return,timesteps)

        if simple_model:
            # print(cur_state)
            a_tensor = model(cur_state)[0]
        else:    
            if state_mean is not None:
                # print(states)
                # print((states-state_mean)/state_std)
                a_tensor = model.get_action(
                    (states.to(dtype=torch.float32) - state_mean) / state_std,
                    actions.to(dtype=torch.float32),
                    rewards.to(dtype=torch.float32),
                    target_return.to(dtype=torch.float32),
                    timesteps.to(dtype=torch.long),
                )
            else:
                a_tensor = model.get_action(
                    (states.to(dtype=torch.float32) - 0) / 1,
                    actions.to(dtype=torch.float32),
                    rewards.to(dtype=torch.float32),
                    target_return.to(dtype=torch.float32),
                    timesteps.to(dtype=torch.long),
                )
        # a_tensor = torch.zeros(4)

        # a_tensor = a_tensor * 2 - 1

        actions[-1] = a_tensor
        # print(a_tensor)

        # not need detach(), because with torch.no_grad() outside
        action = a_tensor.detach().cpu().numpy()
        # print(f'current action is {action}')
        real_action = action
        state, next_state, reward, done = env.step(action)

        pred_return = target_return[0, -1] - (reward/1)
        # pred_return = target_return[0, -1] + (reward/1)
        target_return = torch.cat(
            [target_return, pred_return.reshape(1, 1)], dim=1)
        # print(f'i {i} current target return is {target_return}')
        timesteps = torch.cat([timesteps, torch.ones(
            (1, 1), device=device, dtype=torch.long) * (i+1)], dim=1)

        record_system_info.append([state[0], state[1], state[3], action, real_action, env.battery.SOC(
        ), env.battery.energy_change, next_state[4], next_state[5], next_state[6], env.unbalance, env.operation_cost])

        record_state.append(state)
        record_action.append(real_action)
        record_reward.append(reward)
        record_output.append(env.current_output)
        record_unbalance.append(env.unbalance)
        state = next_state

    # print(actions)
    record_system_info[-1][7:10] = [env.final_step_outputs[0],
                                    env.final_step_outputs[1], env.final_step_outputs[2]]
    # add information of last step soc
    record_system_info[-1][5] = env.final_step_outputs[3]
    record = {'init_info': record_init_info, 'information': record_system_info, 'state': record_state, 'action': record_action,
              'reward': record_reward, 'cost': record_cost, 'unbalance': record_unbalance, 'record_output': record_output}
    return record


def get_episode_return(env, act, device):
    episode_return = 0.0  # sum of rewards in an episode
    episode_unbalance = 0.0
    state = env.reset()
    for i in range(24):
        s_tensor = torch.as_tensor((state,), device=device)
        a_tensor = act(s_tensor)
        # not need detach(), because with torch.no_grad() outside
        action = a_tensor.detach().cpu().numpy()[0]
        state, next_state, reward, done, = env.step(action)
        state = next_state
        episode_return += reward
        episode_unbalance += env.real_unbalance
        if done:
            break
    return episode_return, episode_unbalance


class ReplayBuffer:
    def __init__(self, max_len, state_dim, action_dim, gpu_id=0):
        self.now_len = 0
        self.next_idx = 0
        self.if_full = False
        self.max_len = max_len
        self.data_type = torch.float32
        self.action_dim = action_dim
        self.device = torch.device(f"cuda:{gpu_id}" if (
            torch.cuda.is_available() and (gpu_id >= 0)) else "cpu")

        other_dim = 1 + 1 + self.action_dim
        self.buf_other = torch.empty(
            size=(max_len, other_dim), dtype=self.data_type, device=self.device)

        if isinstance(state_dim, int):  # state is pixel
            self.buf_state = torch.empty(
                (max_len, state_dim), dtype=torch.float32, device=self.device)
        elif isinstance(state_dim, tuple):
            self.buf_state = torch.empty(
                (max_len, *state_dim), dtype=torch.uint8, device=self.device)
        else:
            raise ValueError('state_dim')

    def extend_buffer(self, state, other):  # CPU array to CPU array
        size = len(other)
        next_idx = self.next_idx + size

        if next_idx > self.max_len:
            self.buf_state[self.next_idx:self.max_len] = state[:self.max_len - self.next_idx]
            self.buf_other[self.next_idx:self.max_len] = other[:self.max_len - self.next_idx]
            self.if_full = True

            next_idx = next_idx - self.max_len
            self.buf_state[0:next_idx] = state[-next_idx:]
            self.buf_other[0:next_idx] = other[-next_idx:]
        else:
            self.buf_state[self.next_idx:next_idx] = state
            self.buf_other[self.next_idx:next_idx] = other
        self.next_idx = next_idx

    def sample_batch(self, batch_size) -> tuple:
        indices = rd.randint(self.now_len - 1, size=batch_size)
        r_m_a = self.buf_other[indices]
        return (r_m_a[:, 0:1],
                r_m_a[:, 1:2],
                r_m_a[:, 2:],
                self.buf_state[indices],
                self.buf_state[indices + 1])

    def update_now_len(self):
        self.now_len = self.max_len if self.if_full else self.next_idx

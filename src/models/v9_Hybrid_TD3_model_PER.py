import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import random
import copy
from torch.autograd import Variable
from torch.distributions import MultivariateNormal, Categorical
import datetime
from torch.distributions import Normal, Categorical, MultivariateNormal

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True

class Memory(object):
    def __init__(self, memory_size, transition_lens, device):
        self.device = device
        self.transition_lens = transition_lens # 存储的数据长度
        self.epsilon = 1e-3 # 防止出现zero priority
        self.alpha = 0.6 # 取值范围(0,1)，表示td error对priority的影响
        self.beta = 0.4 # important sample， 从初始值到1
        self.beta_increment_per_sampling = 1e-5
        self.abs_err_upper = 1 # abs_err_upper和epsilon ，表明p优先级值的范围在[epsilon,abs_err_upper]之间，可以控制也可以不控制

        self.memory_size = memory_size
        self.memory_counter = 0

        self.prioritys_ = torch.zeros(size=[memory_size, 2]).to(self.device)
        # indexs = torch.range(0, self.memory_size)
        # self.prioritys_[:, 1] = indexs

        self.memory = torch.zeros(size=[memory_size, transition_lens]).to(self.device)

    def get_priority(self, td_error):
        return torch.pow(torch.abs(td_error) + self.epsilon, self.alpha)

    def add(self, td_error, transitions): # td_error是tensor矩阵
        transition_lens = len(transitions)
        p = td_error

        memory_start = self.memory_counter % self.memory_size
        memory_end = (self.memory_counter + len(transitions)) % self.memory_size

        if memory_end > memory_start:
            self.memory[memory_start: memory_end, :] = transitions
            self.prioritys_[memory_start: memory_end, :] = p
        else:
            replace_len_1 = self.memory_size - memory_start
            self.memory[memory_start: self.memory_size, :] = transitions[0: replace_len_1]
            self.prioritys_[memory_start: self.memory_size, :] = p[0: replace_len_1, :]

            replace_len_2 = transition_lens - replace_len_1
            self.memory[:replace_len_2, :] = transitions[replace_len_1: transition_lens]
            self.prioritys_[:replace_len_2, :] = p[replace_len_1: transition_lens, :]

        self.memory_counter += len(transitions)

    def stochastic_sample(self, batch_size):
        if self.memory_counter >= self.memory_size:
            priorities = self.get_priority(self.prioritys_[:, 0:1])

            total_p = torch.sum(priorities, dim=0)
            min_prob = torch.min(priorities)
            # 采样概率分布
            P = torch.div(priorities, total_p).squeeze(1).cpu().numpy()
            sample_indexs = torch.Tensor(np.random.choice(self.memory_size, batch_size, p=P, replace=False)).long().to(self.device)
        else:
            priorities = self.get_priority(self.prioritys_[:self.memory_counter, 0:1])
            total_p = torch.sum(priorities, dim=0)
            min_prob = torch.min(priorities)
            P = torch.div(priorities, total_p).squeeze(1).cpu().numpy()

            sample_indexs = torch.Tensor(np.random.choice(self.memory_counter, batch_size, p=P, replace=False)).long().to(self.device)

        self.beta = torch.min(torch.FloatTensor([1., self.beta + self.beta_increment_per_sampling])).item()

        batch = self.memory[sample_indexs]
        choose_priorities = priorities[sample_indexs]
        ISweights = torch.pow(torch.div(choose_priorities, min_prob), -self.beta)

        return sample_indexs, batch, ISweights

    def greedy_sample(self, batch_size):
        # total_p = torch.sum(self.prioritys_, dim=0)

        if self.memory_counter >= self.memory_size:
            min_prob = torch.min(self.prioritys_)
        else:
            min_prob = torch.min(self.prioritys_[:self.memory_counter, :])
        self.beta = torch.min(torch.FloatTensor([1., self.beta + self.beta_increment_per_sampling])).item()

        sorted_priorities, sorted_indexs = torch.sort(-self.prioritys_, dim=0)

        choose_idxs = sorted_indexs[:batch_size, :].squeeze(1)

        batch = self.memory[choose_idxs]

        choose_priorities = -sorted_priorities[:batch_size, :]

        ISweights = torch.pow(torch.div(choose_priorities, min_prob), -self.beta).detach()

        return choose_idxs, batch, ISweights

    def batch_update(self, choose_idx, td_errors):
        # p = self.get_priority(td_errors)
        self.prioritys_[choose_idx, 0:1] = td_errors

def hidden_init(layer):
    # source: The other layers were initialized from uniform distributions
    # [− 1/sqrt(f) , 1/sqrt(f) ] where f is the fan-in of the layer
    fan_in = layer.weight.data.size()[0]
    lim = 1. / np.sqrt(fan_in)
    return (0, lim)

class Hybrid_Critic(nn.Module):
    def __init__(self, input_dims, action_nums):
        super(Hybrid_Critic, self).__init__()
        self.input_dims = input_dims
        self.action_nums = action_nums

        deep_input_dims = self.input_dims + self.action_nums * 2

        self.bn_input = nn.BatchNorm1d(self.input_dims)
        self.bn_input.weight.data.fill_(1)
        self.bn_input.bias.data.fill_(0)

        neuron_nums = [300, 200]

        self.mlp_1 = nn.Sequential(
            nn.Linear(deep_input_dims, neuron_nums[0]),
            # nn.BatchNorm1d(neuron_nums[0]),
            nn.ReLU(),
            nn.Linear(neuron_nums[0], neuron_nums[1]),
            # nn.BatchNorm1d(neuron_nums[1]),
            nn.ReLU(),
            nn.Linear(neuron_nums[1], 1)
        )

        self.mlp_2 = nn.Sequential(
            nn.Linear(deep_input_dims, neuron_nums[0]),
            # nn.BatchNorm1d(neuron_nums[0]),
            nn.ReLU(),
            nn.Linear(neuron_nums[0], neuron_nums[1]),
            # nn.BatchNorm1d(neuron_nums[1]),
            nn.ReLU(),
            nn.Linear(neuron_nums[1], 1)
        )

        self.reset_parameters()

    def reset_parameters(self):
        for i in range(3):
            if i % 2 == 0:
                self.mlp_1[i].weight.data.uniform_(*hidden_init(self.mlp_1[i]))
                self.mlp_2[i].weight.data.uniform_(*hidden_init(self.mlp_2[i]))

        # self.mlp_1[1].weight.data.fill_(1)
        # self.mlp_1[1].bias.data.fill_(0)
        #
        # self.mlp_1[4].weight.data.fill_(1)
        # self.mlp_1[4].bias.data.fill_(0)
        #
        # self.mlp_2[1].weight.data.fill_(1)
        # self.mlp_2[1].bias.data.fill_(0)
        #
        # self.mlp_2[4].weight.data.fill_(1)
        # self.mlp_2[4].bias.data.fill_(0)

        self.mlp_1[4].weight.data.uniform_(-0.003, 0.003)
        self.mlp_2[4].weight.data.uniform_(-0.003, 0.003)

    def evaluate(self, input, c_actions, d_actions):
        obs = self.bn_input(input)
        # obs = input
        c_q_out_1 = self.mlp_1(torch.cat([obs, d_actions, c_actions], dim=-1))
        c_q_out_2 = self.mlp_2(torch.cat([obs, d_actions, c_actions], dim=-1))

        return c_q_out_1, c_q_out_2

    def evaluate_q_1(self, input, c_actions, d_actions):
        obs = self.bn_input(input)
        # obs = input
        c_q_out_1 = self.mlp_1(torch.cat([obs, d_actions, c_actions], dim=-1))

        return c_q_out_1

class Hybrid_Actor(nn.Module):
    def __init__(self, input_dims, action_nums):
        super(Hybrid_Actor, self).__init__()
        self.input_dims = input_dims
        self.action_dims = action_nums

        self.bn_input = nn.BatchNorm1d(self.input_dims)
        self.bn_input.weight.data.fill_(1)
        self.bn_input.bias.data.fill_(0)

        neuron_nums = [300, 200]
        self.mlp = nn.Sequential(
            nn.Linear(self.input_dims, neuron_nums[0]),
            # nn.BatchNorm1d(neuron_nums[0]),
            nn.ReLU(),
            nn.Linear(neuron_nums[0], neuron_nums[1]),
            # nn.BatchNorm1d(neuron_nums[1]),
            nn.ReLU()
        )# 特征提取层

        self.c_action_layer = nn.Sequential(
            nn.Linear(neuron_nums[1], self.action_dims)
        )
        self.d_action_layer = nn.Sequential(
            nn.Linear(neuron_nums[1], self.action_dims)
        )

        self.reset_parameters()

    def reset_parameters(self):
        for i in range(3):
            if i % 2 == 0:
                self.mlp[i].weight.data.uniform_(*hidden_init(self.mlp[i]))

        # self.c_action_layer[0].weight.data.uniform_(-0.003, 0.003)
        # self.d_action_layer[0].weight.data.uniform_(-0.003, 0.003)

    def act(self, input, temprature):
        obs = self.bn_input(input)
        # obs = input
        feature_exact = self.mlp(obs)

        c_action_means = self.c_action_layer(feature_exact)
        c_action_means = torch.softmax(c_action_means + torch.normal(c_action_means, 0.1).detach(), dim=-1)
        # c_action_means = boltzmann_softmax(c_action_means + torch.normal(c_action_means, 0.1).detach(), temprature)

        d_action_q_values = self.d_action_layer(feature_exact)

        d_action = gumbel_softmax_sample(logits=d_action_q_values + torch.normal(d_action_q_values, 0.1).detach(),
                                         temprature=temprature, hard=False)
        # print(d_action)
        ensemble_d_actions = torch.argmax(d_action, dim=-1) + 1
        # print(ensemble_d_actions)

        return c_action_means, c_action_means, d_action, ensemble_d_actions.view(-1, 1)

    def evaluate(self, input):
        obs = self.bn_input(input)
        # obs = input
        feature_exact = self.mlp(obs)

        c_actions = self.c_action_layer(feature_exact)
        d_actions = self.d_action_layer(feature_exact)

        return c_actions, d_actions

def boltzmann_softmax(actions, temprature):
    return (actions / temprature).exp() / torch.sum((actions / temprature).exp(), dim=-1).view(-1, 1)

def gumbel_softmax_sample(logits, temprature=1.0, hard=True, eps=1e-20, uniform_seed=1.0):
    U = Variable(torch.FloatTensor(*logits.shape).uniform_().cuda(), requires_grad=False)
    y = logits + -torch.log(-torch.log(U + eps) + eps)
    y = F.softmax(y / temprature, dim=-1)

    if hard:
        y_hard = onehot_from_logits(y)
        y = (y_hard - y).detach() + y

    return y

def onehot_from_logits(logits, eps=0.0):
    """
    Given batch of logits, return one-hot sample using epsilon greedy strategy
    (based on given epsilon)
    """
    # get best (according to current policy) actions in one-hot form
    argmax_acs = (logits == logits.max(1, keepdim=True)[0]).float()
    if eps == 0.0:
        return argmax_acs
    # get random actions in one-hot form
    rand_acs = Variable(torch.eye(logits.shape[1])[[np.random.choice(
        range(logits.shape[1]), size=logits.shape[0])]], requires_grad=False)
    # chooses between best and random actions using epsilon greedy
    return torch.stack([argmax_acs[i] if r > eps else rand_acs[i] for i, r in
                        enumerate(torch.rand(logits.shape[0]))])

class Hybrid_TD3_Model():
    def __init__(
            self,
            feature_nums,
            field_nums=15,
            latent_dims=5,
            action_nums=2,
            campaign_id='1458',
            lr_C_A=1e-3,
            lr_D_A=1e-3,
            lr_C=1e-2,
            data_len=10,
            train_batch_size=10,
            reward_decay=1.0,
            memory_size=4096000,
            batch_size=256,
            tau=0.01,  # for target network soft update
            device='cuda:0',
    ):
        self.feature_nums = feature_nums
        self.field_nums = field_nums
        self.action_nums = action_nums
        self.campaign_id = campaign_id
        self.lr_C_A = lr_C_A
        self.lr_D_A = lr_D_A
        self.lr_C = lr_C
        self.data_len = data_len
        self.train_batch_size = train_batch_size
        self.gamma = reward_decay
        self.latent_dims = latent_dims
        self.memory_size = memory_size
        self.batch_size = batch_size
        self.tau = tau
        self.device = device

        setup_seed(1)

        self.memory_counter = 0

        self.input_dims = self.field_nums * (self.field_nums - 1) // 2 + self.field_nums * self.latent_dims

        self.memory = Memory(self.memory_size, self.field_nums + self.action_nums * 2 + 2, self.device)

        self.Hybrid_Actor = Hybrid_Actor(self.input_dims, self.action_nums).to(self.device)
        self.Hybrid_Critic = Hybrid_Critic(self.input_dims, self.action_nums).to(self.device)

        self.Hybrid_Actor_ = copy.deepcopy(self.Hybrid_Actor)
        self.Hybrid_Critic_ = copy.deepcopy(self.Hybrid_Critic)

        # 优化器
        self.optimizer_a = torch.optim.Adam(self.Hybrid_Actor.parameters(), lr=self.lr_C_A)
        self.optimizer_c = torch.optim.Adam(self.Hybrid_Critic.parameters(), lr=self.lr_C)

        self.loss_func = nn.MSELoss(reduction='mean')

        self.learn_iter = 0
        self.policy_freq = 2

        self.temprature = 1.0
        self.temprature_min = 0.1
        self.anneal_rate = 1e-6

    def store_transition(self, transitions): # 所有的值都应该弄成float
        if torch.max(self.memory.prioritys_) == 0.:
            td_errors = torch.cat([torch.ones(size=[len(transitions), 1]).to(self.device), transitions[:, -1].view(-1, 1)], dim=-1)
        else:
            td_errors = torch.cat([torch.max(self.memory.prioritys_).expand_as(torch.ones(size=[len(transitions), 1])).to(self.device), transitions[:, -1].view(-1, 1)], dim=-1)
    #
        self.memory.add(td_errors, transitions)

    # def store_transition(self, transitions):  # 所有的值都应该弄成float
    #     labels = transitions[:, -1].view(-1, 1)
    #     if torch.max(self.memory.prioritys_) == 0.:
    #         priority_seeds = torch.cat([torch.ones(size=[len(transitions), 1]).to(self.device), labels], dim=-1)
    #     else:
    #         current_priority_seeds = torch.ones(size=[len(transitions), 2]).to(self.device)
    #
    #         label_clk = (labels == 1).nonzero()[:, 0]
    #         if len(label_clk) > 0:
    #             with_clk_indexs = (self.memory.prioritys_[:, 1:2] == 1).nonzero()[:, 0]
    #             if len(with_clk_indexs) > 0:
    #                 with_clk_prioritys_max = torch.max(self.memory.prioritys_[with_clk_indexs, 0])
    #                 current_priority_seeds[label_clk] = torch.cat(
    #                     [with_clk_prioritys_max.expand_as(torch.ones(size=[len(label_clk), 1])).to(self.device),
    #                      torch.ones(size=[len(label_clk), 1]).to(self.device)], dim=-1)
    #             else:
    #                 current_priority_seeds[label_clk] = torch.cat(
    #                     [torch.ones(size=[len(label_clk), 1]), torch.ones(size=[len(label_clk), 1])], dim=-1).to(
    #                     self.device)
    #
    #         without_clk_indexs = (self.memory.prioritys_[:, 1:2] == 0).nonzero()[:, 0]
    #         without_clk_prioritys_max = torch.max(self.memory.prioritys_[without_clk_indexs, 0])
    #         label_without_clk = (labels == 0).nonzero()[:, 0]
    #         current_priority_seeds[label_without_clk] = torch.cat(
    #             [without_clk_prioritys_max.expand_as(torch.ones(size=[len(label_without_clk), 1])),
    #              torch.zeros(size=[len(label_without_clk), 1]).to(self.device)], dim=-1)
    #
    #         priority_seeds = current_priority_seeds
    #
    #     self.memory.add(priority_seeds, transitions)

    def choose_action(self, state, random):
        self.Hybrid_Actor.eval()
        with torch.no_grad():
            c_actions, ensemble_c_actions, d_q_values, ensemble_d_actions = self.Hybrid_Actor.act(state, self.temprature)

            #if random:
                #c_actions = torch.randn_like(c_actions)
               # ensemble_c_actions = torch.softmax(c_actions, dim=-1)
                # ensemble_c_actions = boltzmann_softmax(c_actions, self.temprature)

              #  d_q_values = onehot_from_logits(torch.softmax(torch.randn_like(d_q_values), dim=-1))
              #  ensemble_d_actions = torch.argmax(d_q_values, dim=-1) + 1

              #  return ensemble_c_actions, ensemble_c_actions, d_q_values, ensemble_d_actions.view(-1, 1)

        self.Hybrid_Actor.train()

        return ensemble_c_actions, ensemble_c_actions, d_q_values, ensemble_d_actions

    def choose_best_action(self, state):
        self.Hybrid_Actor.eval()
        with torch.no_grad():
            c_action_means, d_q_values = self.Hybrid_Actor.evaluate(state)

        ensemble_c_actions = torch.softmax(c_action_means, dim=-1)
        # ensemble_c_actions = boltzmann_softmax(c_action_means, self.temprature)

        ensemble_d_actions = gumbel_softmax_sample(d_q_values, temprature=0.1, hard=True)
        ensemble_d_actions = torch.argmax(ensemble_d_actions, dim=-1) + 1

        return ensemble_d_actions.view(-1, 1), ensemble_c_actions, ensemble_c_actions

    def soft_update(self, net, net_target):
        for param_target, param in zip(net_target.parameters(), net.parameters()):
            param_target.data.copy_(param_target.data * (1.0 - self.tau) + param.data * self.tau)

    def to_next_state_c_actions(self, next_d_actions, next_c_actions):
        choose_d_ = torch.argmax(next_d_actions, dim=-1) + 1

        next_c_actions_with_noise = next_c_actions + torch.normal(next_c_actions, 0.1)
        sort_c_actions, sortindex_c_actions = torch.sort(-next_c_actions_with_noise, dim=-1)

        return_c_actions = torch.zeros(size=sortindex_c_actions.size()).to(self.device)

        for i in range(sortindex_c_actions.size()[1]):
            choose_d_actions_index = (choose_d_ == (i + 1)).nonzero()[:, 0]

            current_choose_c_actions_index = sortindex_c_actions[choose_d_actions_index, :(i + 1)]

            current_next_c_actions = torch.softmax(
                sort_c_actions[choose_d_actions_index, :(i+1)] * -1, dim=-1
            )

            return_c_actions_temp = torch.zeros(size=[choose_d_actions_index.size()[0], sortindex_c_actions.size()[1]]).to(self.device)

            # 按列取取出每一列的选择,直接将softmax后的值复制到对应位置
            for m in range(i + 1):
                current_choose_c_actions_index_row = current_choose_c_actions_index[:, m: m + 1]

                for l in range(sortindex_c_actions.size()[1]):
                    with_choose_index = (current_choose_c_actions_index_row == l).nonzero()[:, 0]
                    current_next_c_actions_temp = current_next_c_actions[with_choose_index, m] # 当前列
                    return_c_actions_temp[with_choose_index, l] = current_next_c_actions_temp

            return_c_actions[choose_d_actions_index, :] = return_c_actions_temp

        return return_c_actions

    def to_current_state_c_actions(self, d_actions, c_actions):
        choose_d_ = torch.argmax(d_actions, dim=-1) + 1

        sort_c_actions, sortindex_c_actions = torch.sort(-c_actions, dim=-1)

        return_c_actions = torch.zeros(size=sortindex_c_actions.size()).to(self.device)

        for i in range(sortindex_c_actions.size()[1]):
            choose_d_actions_index = (choose_d_ == (i + 1)).nonzero()[:, 0]

            current_choose_c_actions_index = sortindex_c_actions[choose_d_actions_index, :(i + 1)]

            current_c_actions = torch.softmax(
                sort_c_actions[choose_d_actions_index, :(i + 1)] * -1, dim=-1
            )

            return_c_actions_temp = torch.zeros(
                size=[choose_d_actions_index.size()[0], sortindex_c_actions.size()[1]]).to(self.device)

            # 按列取取出每一列的选择,直接将softmax后的值复制到对应位置
            for m in range(i + 1):
                current_choose_c_actions_index_row = current_choose_c_actions_index[:, m: m + 1]

                for l in range(sortindex_c_actions.size()[1]):
                    with_choose_index = (current_choose_c_actions_index_row == l).nonzero()[:, 0]
                    current_c_actions_temp = current_c_actions[with_choose_index, m]  # 当前列
                    return_c_actions_temp[with_choose_index, l] = current_c_actions_temp

            return_c_actions[choose_d_actions_index, :] = return_c_actions_temp

        return return_c_actions

    def learn(self, embedding_layer):
        self.learn_iter += 1

        if (self.learn_iter + 1) % 1000 == 0:
            # self.temprature = max(np.exp(-self.anneal_rate * self.learn_iter), 0.5)
            self.temprature = max(self.temprature_min, self.temprature - (self.temprature - self.temprature_min) * self.learn_iter / (self.data_len // self.train_batch_size))

        # sample
        choose_idx, batch_memory, ISweights = self.memory.stochastic_sample(self.batch_size)

        b_s = embedding_layer.forward(batch_memory[:, :self.field_nums].long())
        b_c_a = batch_memory[:, self.field_nums: self.field_nums + self.action_nums]
        b_d_a = batch_memory[:,
                self.field_nums + self.action_nums: self.field_nums + self.action_nums * 2]
        b_discrete_a = torch.unsqueeze(batch_memory[:, self.field_nums + self.action_nums * 2], 1)
        b_r = torch.unsqueeze(batch_memory[:, -1], 1)
        b_s_ = b_s  # embedding_layer.forward(batch_memory_states)

        with torch.no_grad():
            c_actions_means_next, d_actions_q_values_next = self.Hybrid_Actor_.evaluate(b_s_)
            next_d_actions = gumbel_softmax_sample(logits=d_actions_q_values_next + torch.normal(d_actions_q_values_next, 0.1), temprature=self.temprature, hard=False)

            # next_c_actions = self.to_next_state_c_actions(next_d_actions, torch.softmax(c_actions_means_next, dim=-1))
            next_c_actions = self.to_next_state_c_actions(next_d_actions, c_actions_means_next)

            # print('1', next_c_actions)
            q1_target, q2_target = \
                self.Hybrid_Critic_.evaluate(b_s_, next_c_actions, next_d_actions)
            # print(q1_target, q2_target)
            q_target = torch.min(q1_target, q2_target)
            q_target = b_r + self.gamma * q_target

        q1, q2 = self.Hybrid_Critic.evaluate(b_s, b_c_a, b_d_a)

        critic_td_error = (q_target * 2 - q1 - q2).detach() / 2

        critic_loss = (ISweights * (F.mse_loss(q1, q_target, reduction='none') + F.mse_loss(q2, q_target, reduction='none'))).mean()

        self.optimizer_c.zero_grad()
        critic_loss.backward()
        nn.utils.clip_grad_norm_(self.Hybrid_Critic.parameters(), 0.5)
        self.optimizer_c.step()

        critic_loss_r = critic_loss.item()

        self.memory.batch_update(choose_idx, critic_td_error)

        if self.learn_iter % self.policy_freq == 0:
            c_actions_means, d_actions_q_values = self.Hybrid_Actor.evaluate(b_s)
            #print(d_actions_q_values)
            d_actions_q_values_ = gumbel_softmax_sample(logits=d_actions_q_values, temprature=0.1, hard=False)
            c_actions_means_ = self.to_current_state_c_actions(d_actions_q_values_, c_actions_means)
            # print('2', c_actions_means_)
            # print('2', d_actions_q_values)
            # print('3', d_actions_q_values_)

            # Hybrid_Actor
            # c_action_softmax = torch.softmax(c_actions_means, dim=-1)
            # d_action_softmax = torch.softmax(d_actions_q_values, dim=-1)
            # c_reg = -(c_action_softmax.log().mean())
            # d_reg = -(d_action_softmax.log().mean())

            c_reg = (c_actions_means ** 2).mean()
            d_reg = (d_actions_q_values ** 2).mean()

            # print(d_actions_q_values_, c_actions_means_)
            a_critic_value = self.Hybrid_Critic.evaluate_q_1(b_s, c_actions_means_, d_actions_q_values_)
            # c_a_loss = -a_critic_value.mean() + (c_reg + d_reg) * 1e-3
            c_a_loss = (ISweights * ((c_reg + d_reg) * 1e-3 - a_critic_value)).mean()
            # print(d_reg)
            self.optimizer_a.zero_grad()
            c_a_loss.backward()
            nn.utils.clip_grad_norm_(self.Hybrid_Actor.parameters(), 0.5)
            self.optimizer_a.step()

            # for name, parms in self.Hybrid_Actor.d_action_layer.named_parameters():
            #     print('-->name:', name, '-->grad_requirs:', parms.requires_grad, \
            #           ' -->grad_value:', parms.grad)

            self.soft_update(self.Hybrid_Critic, self.Hybrid_Critic_)
            self.soft_update(self.Hybrid_Actor, self.Hybrid_Actor_)

        return critic_loss_r

class OrnsteinUhlenbeckNoise:
    def __init__(self, mu):
        self.theta, self.dt, self.sigma = 0.15, 0.01, 0.2
        self.mu = mu
        self.x_prev = np.zeros_like(self.mu)

    def __call__(self):
        x = self.x_prev + self.theta * (self.mu - self.x_prev) * self.dt + \
            self.sigma * np.sqrt(self.dt) * np.random.normal(size=self.mu.shape)
        self.x_prev = x
        return x

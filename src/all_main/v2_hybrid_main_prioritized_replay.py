import pandas as pd
import numpy as np
import tqdm
import datetime
import os
import argparse
import random
from sklearn.metrics import roc_auc_score
import src.models.p_model as p_model
import src.models.v2_Hybrid_RL_model_Prioritized_Replay as hybrid_rl_model
import src.models.creat_data as Data
from src.models.Feature_embedding import Feature_Embedding

import torch
import torch.nn as nn
import torch.utils.data

np.seterr(all='raise')


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True


def get_model(action_nums, feature_nums, field_nums, latent_dims, init_lr_a, init_lr_c, batch_size, memory_size, device, campaign_id):
    RL_model = hybrid_rl_model.Hybrid_RL_Model(feature_nums, field_nums, latent_dims,
                                               action_nums=action_nums, lr_C_A=init_lr_a, lr_D_A=init_lr_a, lr_C=init_lr_c,
                                               campaign_id=campaign_id, batch_size=64,
                                               memory_size=memory_size, device=device)
    return RL_model


def get_dataset(datapath, dataset_name, campaign_id):
    data_path = datapath + dataset_name + campaign_id
    train_data_file_name = 'train_.txt'
    train_fm = pd.read_csv(data_path + train_data_file_name, header=None).values.astype(int)

    test_data_file_name = 'test_.txt'
    test_fm = pd.read_csv(data_path + test_data_file_name, header=None).values.astype(int)

    field_nums = len(train_fm[0, 1:])  # 特征域的数量

    feature_index_name = 'featindex.txt'
    feature_index = pd.read_csv(data_path + feature_index_name, header=None).values
    feature_nums = int(feature_index[-1, 0].split('\t')[1]) + 1  # 特征数量

    train_data = train_fm
    test_data = test_fm

    return train_fm, train_data, test_data, field_nums, feature_nums

def generate_preds(model_dict, features, actions, prob_weights,
                   labels, device, mode):
    y_preds = torch.ones(size=[len(features), 1]).to(device)
    rewards = torch.ones(size=[len(features), 1]).to(device)

    sort_prob_weights, sortindex_prob_weights = torch.sort(-prob_weights, dim=1)

    pretrain_model_len = len(model_dict)  # 有多少个预训练模型

    pretrain_y_preds = {}
    for i in range(pretrain_model_len):
        pretrain_y_preds[i] = model_dict[i](features).detach()

    choose_model_lens = range(2, pretrain_model_len + 1)
    for i in choose_model_lens:  # 根据ddqn_model的action,判断要选择ensemble的数量
        with_action_indexs = (actions == i).nonzero()[:, 0]
        current_choose_models = sortindex_prob_weights[with_action_indexs][:, :i]
        current_basic_rewards = torch.ones(size=[len(with_action_indexs), 1]).to(device) * 1
        current_prob_weights = prob_weights[with_action_indexs]

        current_with_clk_indexs = (labels[with_action_indexs] == 1).nonzero()[:, 0]
        current_without_clk_indexs = (labels[with_action_indexs] == 0).nonzero()[:, 0]

        current_pretrain_y_preds = torch.cat([
            pretrain_y_preds[l][with_action_indexs] for l in range(pretrain_model_len)
        ], dim=1)

        if i == pretrain_model_len:
            current_y_preds = torch.sum(torch.mul(current_prob_weights, current_pretrain_y_preds), dim=1).view(-1, 1)
            y_preds[with_action_indexs, :] = current_y_preds
        elif i == 1:
            current_y_preds = current_pretrain_y_preds[current_choose_models]
            y_preds = current_y_preds
        else:
            current_pretrain_y_preds = torch.cat([
                pretrain_y_preds[l][with_action_indexs] for l in range(pretrain_model_len)
            ], dim=1)

            current_softmax_weights = torch.softmax(
                sort_prob_weights[with_action_indexs][:, :i] * -1, dim=1
            ).to(device)  # 再进行softmax

            current_row_preds = torch.ones(size=[len(with_action_indexs), i]).to(device)

            for m in range(i):
                current_row_choose_models = current_choose_models[:, m:m + 1]
                for k in range(pretrain_model_len):
                    current_pretrain_y_pred = pretrain_y_preds[k][with_action_indexs]
                    choose_model_indexs = (current_row_choose_models == k).nonzero()[:, 0]

                    current_row_preds[choose_model_indexs, m:m + 1] = current_pretrain_y_pred[choose_model_indexs]

            current_y_preds = torch.sum(torch.mul(current_softmax_weights, current_row_preds), dim=1).view(-1, 1)
            y_preds[with_action_indexs, :] = current_y_preds

        with_clk_rewards = torch.where(
            current_y_preds[current_with_clk_indexs] >= current_pretrain_y_preds[
                current_with_clk_indexs].mean(dim=1).view(-1, 1),
            current_basic_rewards[current_with_clk_indexs] * 1,
            current_basic_rewards[current_with_clk_indexs] * 0
        )

        without_clk_rewards = torch.where(
            current_y_preds[current_without_clk_indexs] <= current_pretrain_y_preds[
                current_without_clk_indexs].mean(dim=1).view(-1, 1),
            current_basic_rewards[current_without_clk_indexs] * 1,
            current_basic_rewards[current_without_clk_indexs] * 0
        )

        current_basic_rewards[current_with_clk_indexs] = with_clk_rewards
        current_basic_rewards[current_without_clk_indexs] = without_clk_rewards

        rewards[with_action_indexs, :] = current_basic_rewards

    return y_preds, rewards


def train(rl_model, model_dict, data_loader, embedding_layer, exploration_rate, device):
    total_critic_loss = 0
    total_actor_loss = 0
    log_intervals = 0
    learn_steps = 0
    total_rewards = 0
    targets, predicts = list(), list()

    for i, (features, labels) in enumerate(tqdm.tqdm(data_loader, smoothing=0, mininterval=1.0)):
        features, labels = features.long().to(device), torch.unsqueeze(labels, 1).to(device)

        embedding_vectors = embedding_layer.forward(features)

        c_actions, ensemble_c_actions, d_q_values, ensemble_d_actions = rl_model.choose_action(embedding_vectors, exploration_rate)

        y_preds, rewards = \
            generate_preds(model_dict, features, ensemble_d_actions, ensemble_c_actions, labels, device, mode='train')

        targets.extend(labels.tolist())  # extend() 函数用于在列表末尾一次性追加另一个序列中的多个值（用新列表扩展原来的列表）。
        predicts.extend(y_preds.tolist())

        transitions = torch.cat([features.float(), c_actions, d_q_values, ensemble_d_actions.float(), rewards], dim=1)

        rl_model.store_transition(transitions, embedding_layer)

        choose_idx, b_s, b_c_a, b_d_a, b_discrete_a, b_r, b_s_, ISweights = rl_model.sample_batch(embedding_layer)

        critic_loss = rl_model.learn_c(choose_idx, b_s, b_c_a, b_d_a, b_discrete_a, b_r, b_s_, ISweights)

        if i % 3 == 0:
            actor_loss = rl_model.learn_a(b_s)
            rl_model.soft_update(rl_model.Hybrid_Actor, rl_model.Hybrid_Actor_)
            rl_model.soft_update(rl_model.Critic, rl_model.Critic_)

        learn_steps += 1
        total_critic_loss = critic_loss
        total_actor_loss = actor_loss
        log_intervals += 1
        total_rewards += torch.sum(rewards, dim=0).item()

        torch.cuda.empty_cache()  # 清除缓存

    return total_critic_loss, total_actor_loss, total_rewards / log_intervals, \
           roc_auc_score(targets, predicts), learn_steps


def test(rl_model, model_dict, embedding_layer, data_loader, loss, device):
    targets, predicts = list(), list()
    intervals = 0
    total_test_loss = 0
    with torch.no_grad():
        for i, (features, labels) in enumerate(data_loader):
            features, labels = features.long().to(device), torch.unsqueeze(labels, 1).to(device)

            embedding_vectors = embedding_layer.forward(features)

            actions, prob_weights = rl_model.choose_best_action(embedding_vectors)

            y, rewards = generate_preds(model_dict, features, actions, prob_weights,
                                                          labels, device, mode='test')

            test_loss = loss(y, labels.float())
            targets.extend(labels.tolist())  # extend() 函数用于在列表末尾一次性追加另一个序列中的多个值（用新列表扩展原来的列表）。
            predicts.extend(y.tolist())
            intervals += 1
            total_test_loss += test_loss.item()

    return roc_auc_score(targets, predicts), total_test_loss / intervals


def submission(rl_model, model_dict, embedding_layer, data_loader, device):
    targets, predicts = list(), list()
    final_actions = torch.LongTensor().to(device)
    final_prob_weights = torch.FloatTensor().to(device)
    with torch.no_grad():
        for features, labels in data_loader:
            features, labels = features.long().to(device), torch.unsqueeze(labels, 1).to(device)

            embedding_vectors = embedding_layer.forward(features)

            actions, prob_weights = rl_model.choose_best_action(embedding_vectors)

            y, rewards = generate_preds(model_dict, features, actions, prob_weights,
                                                          labels, device, mode='test')

            targets.extend(labels.tolist())  # extend() 函数用于在列表末尾一次性追加另一个序列中的多个值（用新列表扩展原来的列表）。
            predicts.extend(y.tolist())

            final_actions = torch.cat([final_actions, actions], dim=0)
            final_prob_weights = torch.cat([final_prob_weights, prob_weights], dim=0)

    return predicts, roc_auc_score(targets, predicts), final_actions.cpu().numpy(), final_prob_weights.cpu().numpy()


def main(data_path, dataset_name, campaign_id, latent_dims, model_name,
         init_lr_a, end_lr_a, init_lr_c, end_lr_c, init_exploration_rate, end_exploration_rate,
         epoch, batch_size, device, save_param_dir):
    if not os.path.exists(save_param_dir):
        os.mkdir(save_param_dir)

    device = torch.device(device)  # 指定运行设备
    train_fm, train_data, test_data, field_nums, feature_nums = get_dataset(data_path, dataset_name, campaign_id)

    train_dataset = Data.libsvm_dataset(train_data[:, 1:], train_data[:, 0])
    test_dataset = Data.libsvm_dataset(test_data[:, 1:], test_data[:, 0])

    train_data_loader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size,
                                                    num_workers=8)  # 0.7153541503790021
    test_data_loader = torch.utils.data.DataLoader(test_dataset, batch_size=batch_size, num_workers=8)

    FFM = p_model.FFM(feature_nums, field_nums, latent_dims)
    FFM_pretrain_params = torch.load(save_param_dir + campaign_id + 'FFMbest.pth')
    FFM.load_state_dict(FFM_pretrain_params)
    FFM.eval()

    LR = p_model.LR(feature_nums)
    LR_pretrain_params = torch.load(save_param_dir + campaign_id + 'LRbest.pth')
    LR.load_state_dict(LR_pretrain_params)
    LR.eval()

    FM = p_model.FM(feature_nums, latent_dims)
    FM_pretrain_params = torch.load(save_param_dir + campaign_id + 'FMbest.pth')
    FM.load_state_dict(FM_pretrain_params)
    FM.eval()

    # AFM = p_model.AFM(feature_nums, field_nums, latent_dims)
    # AFM_pretrain_params = torch.load(save_param_dir + campaign_id + 'AFMbest.pth')
    # AFM.load_state_dict(AFM_pretrain_params)
    # AFM.eval()
    #
    # WandD = p_model.WideAndDeep(feature_nums, field_nums, latent_dims)
    # WandD_pretrain_params = torch.load(save_param_dir + campaign_id + 'W&Dbest.pth')
    # WandD.load_state_dict(WandD_pretrain_params)
    # WandD.eval()
    #
    # DeepFM = p_model.DeepFM(feature_nums, field_nums, latent_dims)
    # DeepFM_pretrain_params = torch.load(save_param_dir + campaign_id + 'DeepFMbest.pth')
    # DeepFM.load_state_dict(DeepFM_pretrain_params)
    # DeepFM.eval()
    #
    # FNN = p_model.FNN(feature_nums, field_nums, latent_dims)
    # FNN_pretrain_params = torch.load(save_param_dir + campaign_id + 'FNNbest.pth')
    # FNN.load_state_dict(FNN_pretrain_params)
    # FNN.eval()
    #
    # IPNN = p_model.InnerPNN(feature_nums, field_nums, latent_dims)
    # IPNN_pretrain_params = torch.load(save_param_dir + campaign_id + 'IPNNbest.pth')
    # IPNN.load_state_dict(IPNN_pretrain_params)
    # IPNN.eval()
    #
    # OPNN = p_model.OuterPNN(feature_nums, field_nums, latent_dims)
    # OPNN_pretrain_params = torch.load(save_param_dir + campaign_id + 'OPNNbest.pth')
    # OPNN.load_state_dict(OPNN_pretrain_params)
    # OPNN.eval()
    #
    # DCN = p_model.DCN(feature_nums, field_nums, latent_dims)
    # DCN_pretrain_params = torch.load(save_param_dir + campaign_id + 'DCNbest.pth')
    # DCN.load_state_dict(DCN_pretrain_params)
    # DCN.eval()

    model_dict = {0: LR.to(device), 1: FM.to(device), 2: FFM.to(device)}
    # model_dict = {0: WandD.to(device), 1: DeepFM.to(device), 2: IPNN.to(device), 3: DCN.to(device), 4: AFM.to(device)}

    model_dict_len = len(model_dict)

    memory_size = 1000000
    rl_model = get_model(model_dict_len, feature_nums, field_nums, latent_dims, init_lr_a, init_lr_c, batch_size,
                                              memory_size, device, campaign_id)

    embedding_layer = Feature_Embedding(feature_nums, field_nums, latent_dims).to(device)
    embedding_layer.load_embedding(FM_pretrain_params)

    loss = nn.BCELoss()

    valid_aucs = []
    valid_losses = []

    start_time = datetime.datetime.now()
    exploration_rate = init_exploration_rate

    rewards_records = []
    global_steps = 0
    for epoch_i in range(epoch):
        torch.cuda.empty_cache()  # 清理无用的cuda中间变量缓存

        train_start_time = datetime.datetime.now()

        train_critic_loss, train_actor_loss, train_average_rewards, train_auc, learn_steps = train(rl_model, model_dict,
                                                                     train_data_loader, embedding_layer,
                                                                     exploration_rate, device)

        # rl_model.optimizer_c.param_groups[0]['lr'] = max(init_lr_c - epoch_i * (init_lr_c - end_lr_c) / (epoch - 100), end_lr_c)
        # rl_model.optimizer_c_a.param_groups[0]['lr'] = max(init_lr_a - epoch_i * (init_lr_a - end_lr_a) / (epoch - 100), end_lr_a)
        # rl_model.optimizer_d_a.param_groups[0]['lr'] = max(init_lr_a - epoch_i * (init_lr_a - end_lr_a) / (epoch - 100), end_lr_a)

        if (epoch_i + 1) % 10 == 0:
            exploration_rate = max(init_exploration_rate - (init_exploration_rate - end_exploration_rate) / (epoch - epoch // 10), end_exploration_rate)

        rewards_records.append(train_average_rewards)

        auc, valid_loss = test(rl_model, model_dict, embedding_layer, test_data_loader, loss,
                               device)
        valid_aucs.append(auc)
        valid_losses.append(valid_loss)

        train_end_time = datetime.datetime.now()
        global_steps += learn_steps
        print('epoch:', epoch_i, 'global_steps:', global_steps, 'training critic loss:', train_critic_loss, 'training actor loss:', train_actor_loss,
              'training average rewards',
              train_average_rewards, 'training auc', train_auc, 'validation auc:', auc,
              'validation loss:', valid_loss, '[{}s]'.format((train_end_time - train_start_time).seconds))

    end_time = datetime.datetime.now()

    test_rl_model = rl_model

    auc, test_loss = test(test_rl_model, model_dict, embedding_layer, test_data_loader, loss,
                          device)
    print('\ntest auc:', auc, datetime.datetime.now(), '[{}s]'.format((end_time - start_time).seconds))

    submission_path = data_path + dataset_name + campaign_id + model_name + '/'  # ctr 预测结果存放文件夹位置
    if not os.path.exists(submission_path):
        os.mkdir(submission_path)

    # 测试集submission
    test_predicts, test_auc, actions, prob_weights = submission(test_rl_model, model_dict,
                                                                embedding_layer, test_data_loader, device)

    prob_weights_df = pd.DataFrame(data=prob_weights)
    prob_weights_df.to_csv(submission_path + 'test_prob_weights.csv', header=None)

    actions_df = pd.DataFrame(data=actions)
    actions_df.to_csv(submission_path + 'test_actions.csv', header=None)

    test_pred_df = pd.DataFrame(data=test_predicts)

    test_pred_df.to_csv(submission_path + 'test_submission.csv', header=None)

    day_aucs = [[test_auc]]
    day_aucs_df = pd.DataFrame(data=day_aucs)
    day_aucs_df.to_csv(submission_path + 'day_aucs.csv', header=None)

    torch.save(test_rl_model.Hybrid_Actor.state_dict(),
               save_param_dir + campaign_id + '/actor_model' + 'best.pth')  # 存储最优参数

    rewards_records_df = pd.DataFrame(data=rewards_records)
    rewards_records_df.to_csv(submission_path + 'train_rewards.csv', header=None)


def eva_stopping(valid_aucs, valid_losses, type):  # early stopping
    if type == 'auc':
        if len(valid_aucs) > 5:
            if valid_aucs[-1] < valid_aucs[-2] and valid_aucs[-2] < valid_aucs[-3] and valid_aucs[-3] < valid_aucs[
                -4] and valid_aucs[-4] < valid_aucs[-5]:
                return True
    else:
        if len(valid_losses) > 5:
            if valid_losses[-1] > valid_losses[-2] and valid_losses[-2] > valid_losses[-3] and valid_losses[-3] > \
                    valid_losses[-4] and valid_losses[-4] > valid_losses[-5]:
                return True

    return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_path', default='../../data/')
    parser.add_argument('--dataset_name', default='ipinyou/', help='ipinyou, cretio, yoyi')
    parser.add_argument('--campaign_id', default='3358/', help='1458, 3386')
    parser.add_argument('--model_name', default='Hybrid_RL_v2', help='LR, FM, FFM, W&D')
    parser.add_argument('--latent_dims', default=10)
    parser.add_argument('--epoch', type=int, default=300)
    parser.add_argument('--init_lr_a', type=float, default=1e-4)
    parser.add_argument('--end_lr_a', type=float, default=1e-4)
    parser.add_argument('--init_lr_c', type=float, default=1e-3)
    parser.add_argument('--end_lr_c', type=float, default=3e-4)
    parser.add_argument('--init_exploration_rate', type=float, default=1)
    parser.add_argument('--end_exploration_rate', type=float, default=0.1)
    parser.add_argument('--weight_decay', type=float, default=1e-5)
    parser.add_argument('--early_stop_type', default='auc', help='auc, loss')
    parser.add_argument('--batch_size', type=int, default=2048)
    parser.add_argument('--device', default='cuda:0')
    parser.add_argument('--save_param_dir', default='../models/model_params/')

    args = parser.parse_args()

    # 设置随机数种子
    setup_seed(1)

    main(
        args.data_path,
        args.dataset_name,
        args.campaign_id,
        args.latent_dims,
        args.model_name,
        args.init_lr_a,
        args.end_lr_a,
        args.init_lr_c,
        args.end_lr_c,
        args.init_exploration_rate,
        args.end_exploration_rate,
        args.epoch,
        args.batch_size,
        args.device,
        args.save_param_dir
    )
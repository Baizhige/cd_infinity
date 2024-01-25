import os
import sys
from torch.optim import SGD
import torch.utils.data
import torch.nn.functional as F
import numpy as np
from my_utils.data_loader_npy import EEGDataSet
from my_utils.model_EEG_Infinity002_any_backbone import EEG_Infinity
from my_utils.test_MengData_new import test
from my_utils.my_tool import CustomLRScheduler, generate_normalized_tensor_eye
from my_utils.recorder import append_results_to_csv
from my_utils.INTEL import MinNormSolver, gradient_normalizers
from torch.utils.tensorboard import SummaryWriter
import configparser
import argparse
from torch.autograd import Variable

# ====
def steal_gradients_target_transfer_matrix(model):
    '''
    返回loss给model，并且截取保存指定参数的梯度，清空指定参数的梯度。
    Args:
        loss: 要计算梯度的loss
        model: 传入的网络参数
    Returns:
        返回“共同参数”的梯度
        共同参数包括：
        -- domain_filter
        -- channel_transfer_matrix
        -- domain_filter
    '''
    gradients = [Variable(model.alignment_head_target.channel_transfer_matrix.grad.data.clone(), requires_grad=False)]
    model.alignment_head_target.channel_transfer_matrix.grad.zero_()
    return gradients

def steal_gradients_feature_extractor(model):
    '''
    返回loss给model，并且截取保存feature module下所有参数的梯度，清空这些参数的梯度。
    Args:
        loss: 要计算梯度的loss
        model: 传入的网络参数
    Returns:
        返回feature module下所有参数的梯度
    '''
    # 截取并保存feature module下所有参数的梯度
    gradients = []
    for param in model.feature.parameters():
        if param.grad is not None:
            gradients.append(Variable(param.grad.data.clone(), requires_grad=False))
            # 清空梯度
            param.grad.zero_()
    return gradients

def cov_loss_cos_distance(tensorA, tensorB):
    """
    计算两组 tensor（tensorA 和 tensorB）的平均协方差矩阵之间的余弦距离。

    tensorA 和 tensorB 都是形状为 (batchsize, 1, c, T) 的 tensor。
    每个 tensor 包含 batchsize 个样本，每个样本是一个 c 行 T 列的矩阵。
    """

    def compute_mean_covariance(input_tensor):
        # 删除大小为 1 的维度，使 tensor 形状变为 (batchsize, c, T)
        input_tensor = input_tensor.squeeze(1)

        # 数据中心化：每个特征减去其均值
        mean = input_tensor.mean(dim=-1, keepdim=True)
        input_tensor_centered = input_tensor - mean

        # 计算协方差矩阵
        covariance_matrices = torch.matmul(input_tensor_centered, input_tensor_centered.transpose(1, 2)) / (
                    input_tensor_centered.shape[-1] - 1)

        # 计算平均协方差矩阵
        mean_covariance = covariance_matrices.mean(dim=0)
        return mean_covariance

    # 计算两个 tensor 的平均协方差矩阵
    mean_covariance_A = compute_mean_covariance(tensorA)
    mean_covariance_B = compute_mean_covariance(tensorB)

    A_normalized = F.normalize(mean_covariance_A, p=2, dim=1)
    B_normalized = F.normalize(mean_covariance_B, p=2, dim=1)


    cosine_similarity = torch.sum(A_normalized * B_normalized, dim=1)

    loss = 1 - cosine_similarity
    return torch.mean(loss)
minnormsolver = MinNormSolver()
parser = argparse.ArgumentParser(description='Read configuration file.')
parser.add_argument('--config', default='config_PhysioNetMIToMengExp12.ini', help='Path to the config.ini file')
parser.add_argument('--cache_prefix', default='parser_test2', help='prefix of the cache (IMPORTANT!)')
parser.add_argument('--prior_information', default='1', help='if the prior_information is used')
parser.add_argument('--backbone_type', default='InceptionEEG', help='choose the backbone type for feature extractor: EEGNet,ShallowConvNet,DeepConvNet,InceptionEEG,EEGSym')

args = parser.parse_args()

config = configparser.ConfigParser()
config.read(os.path.join("hyperparameters", args.config))

cache_prefix = args.cache_prefix
NFold = config.getint('settings', 'NFold')
record_val_metric = np.zeros([2, NFold])
record_test_metric = np.zeros([2, NFold])
n_epoch = config.getint('settings', 'n_epoch')
print(cache_prefix)
for cross_id in range(NFold):
    print("Cross validation {0}-fold".format(cross_id))
    model_root = 'models'
    cuda = True
    writer = SummaryWriter(os.path.join('logs', cache_prefix + '_Cross_{0}'.format(cross_id)))
    # load data
    source_eeg_root = os.path.join(os.path.pardir, os.path.pardir, "EEGData")
    source_train_list = [os.path.join(config.get('settings', 'source_path'), "concatedData", "train",
                                      "cross_{0}_".format(cross_id) + config.get('settings', 'source_datafile_name')),
                         os.path.join(config.get('settings', 'source_path'), "concatedData", "train",
                                      "cross_{0}_".format(cross_id) + config.get('settings', 'source_labelfile_name'))]
    source_eval_list = [os.path.join(config.get('settings', 'source_path'), "concatedData", "eval",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'source_datafile_name')),
                        os.path.join(config.get('settings', 'source_path'), "concatedData", "eval",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'source_labelfile_name'))]
    source_test_list = [os.path.join(config.get('settings', 'source_path'), "concatedData", "test",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'source_datafile_name')),
                        os.path.join(config.get('settings', 'source_path'), "concatedData", "test",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'source_labelfile_name'))]

    target_eeg_root = os.path.join(os.path.pardir, os.path.pardir, "EEGData")

    target_train_list = [os.path.join(config.get('settings', 'target_path'), "concatedData", "train",
                                      "cross_{0}_".format(cross_id) + config.get('settings', 'target_datafile_name')),
                         os.path.join(config.get('settings', 'target_path'), "concatedData", "train",
                                      "cross_{0}_".format(cross_id) + config.get('settings', 'target_labelfile_name'))]
    target_eval_list = [os.path.join(config.get('settings', 'target_path'), "concatedData", "eval",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'target_datafile_name')),
                        os.path.join(config.get('settings', 'target_path'), "concatedData", "eval",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'target_labelfile_name'))]
    target_test_list = [os.path.join(config.get('settings', 'target_path'), "concatedData", "test",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'target_datafile_name')),
                        os.path.join(config.get('settings', 'target_path'), "concatedData", "test",
                                     "cross_{0}_".format(cross_id) + config.get('settings', 'target_labelfile_name'))]

    print("Source dataset")
    print(source_train_list)
    print(source_eval_list)
    print(source_test_list)
    print("Target dataset")
    print(target_train_list)
    print(target_eval_list)
    print(target_test_list)

    source_train_dataset = EEGDataSet(
        data_root=source_eeg_root,
        data_list=source_train_list,
        num_channel=config.getint('settings', 'source_num_channel'),
        datalen=config.getint('settings', 'source_datalen')
    )
    source_train_dataloader = torch.utils.data.DataLoader(
        dataset=source_train_dataset,
        batch_size=config.getint('settings', 'batch_size'),
        shuffle=True,
        num_workers=4)

    target_train_dataset = EEGDataSet(
        data_root=target_eeg_root,
        data_list=target_train_list,
        num_channel=config.getint('settings', 'target_num_channel'),
        datalen=config.getint('settings', 'target_datalen')
    )

    target_train_dataloader = torch.utils.data.DataLoader(
        dataset=target_train_dataset,
        batch_size=config.getint('settings', 'batch_size'),
        shuffle=True,
        num_workers=4)

    # load model
    CAR_matrix_source = torch.eye(config.getint('settings', 'source_num_channel')) - torch.ones(
        [config.getint('settings', 'source_num_channel'),
         config.getint('settings', 'source_num_channel')]) / config.getint('settings', 'source_num_channel')

    transfer_matrix_source = CAR_matrix_source.cuda()

    with torch.no_grad():
        transfer_matrix_source_inv = torch.inverse(transfer_matrix_source)

    CAR_matrix_target = torch.eye(config.getint('settings', 'target_num_channel')) - torch.ones(
        [config.getint('settings', 'target_num_channel'),
         config.getint('settings', 'target_num_channel')]) / config.getint('settings', 'target_num_channel')
    transfer_matrix_target = torch.matmul(
        torch.tensor(np.load(os.path.join('config', config.get('settings', 'file_name_transfer_matrix')))).to(
            torch.float32), CAR_matrix_target).cuda()
    _right_idx_ = torch.tensor(np.load(os.path.join("config", config.get('settings', 'right_idx')))-1).cuda()
    _left_idx_ = torch.tensor(np.load(os.path.join("config", config.get('settings', 'left_idx')))-1).cuda()


    if args.prior_information == '1':
        my_net = EEG_Infinity(transfer_matrix_source, transfer_matrix_target, num_channels=config.getint('settings', 'source_num_channel'), FIR_order=17, backbone_type=args.backbone_type, right_idx=_right_idx_, left_idx=_left_idx_)
        print("prior_information used!")
    else:
        print("no prior_information used!")
        transfer_matrix_source_random = generate_normalized_tensor_eye(config.getint('settings', 'source_num_channel'),
                                                                       config.getint('settings', 'source_num_channel'))
        transfer_matrix_target_random = generate_normalized_tensor_eye(config.getint('settings', 'source_num_channel'),
                                                                       config.getint('settings', 'target_num_channel'))
        my_net = EEG_Infinity(transfer_matrix_source_random, transfer_matrix_target_random, num_channels=config.getint('settings', 'source_num_channel'), FIR_order=17, backbone_type=args.backbone_type, right_idx=_right_idx_, left_idx=_left_idx_)
    # setup optimizer
    len_dataloader = min(len(target_train_dataloader), len(source_train_dataloader))
    total_steps = n_epoch * len_dataloader
    optimizer = SGD(my_net.parameters(), lr=config.getfloat('optimizer', 'lr'),
                    momentum=config.getfloat('optimizer', 'momentum'))
    scheduler = CustomLRScheduler(optimizer, mu=config.getfloat('optimizer', 'mu'),
                                  alpha=config.getfloat('optimizer', 'alpha'),
                                  beta=config.getfloat('optimizer', 'beta'), total_steps=total_steps)

    # 两个negative log loss
    loss_class = torch.nn.NLLLoss()
    loss_domain = torch.nn.NLLLoss()
    my_LogSoftmax = torch.nn.LogSoftmax(dim=1)

    if cuda:
        my_net = my_net.cuda()
        loss_class = loss_class.cuda()
        loss_domain = loss_domain.cuda()
        my_LogSoftmax = my_LogSoftmax.cuda()

    # training
    best_acc_source = 0.0
    best_index_source = 0

    best_acc_target = 0.0
    best_index_target = 0

    for epoch in range(n_epoch):
        # 每个epoch做如下事情
        data_source_iter = iter(source_train_dataloader)
        data_target_iter = iter(target_train_dataloader)
        my_net.train()
        for i in range(len_dataloader):
            my_net.zero_grad()
            # 正向传播源域数据
            data_source = next(data_source_iter)

            s_eeg, s_subject, s_label = data_source

            s_domain_label = torch.ones(len(s_label)).long()
            if cuda:
                s_eeg = s_eeg.cuda()
                s_label = s_label.cuda()
                s_domain_label = s_domain_label.cuda()

            # 正向传播目标域数据
            data_target = next(data_target_iter)
            t_eeg, t_subject, t_label = data_target

            t_domain_label = torch.zeros(len(t_label)).long()

            if cuda:
                t_eeg = t_eeg.cuda()
                t_label = t_label.cuda()
                t_domain_label = t_domain_label.cuda()

            if len(s_label) != len(t_label):
                # 在最后一个iter时，可能会有源域和目标域的数量不对称，直接pass
                continue

            s_class_output, s_domain_output, s_spatial_output, s_filter_output = my_net(input_data=s_eeg, domain=0, alpha=1)
            # 源域的分类误差
            err_s_label = loss_class(my_LogSoftmax(s_class_output), s_label.long())
            err_s_domain = loss_domain(my_LogSoftmax(s_domain_output), s_domain_label)


            t_class_output, t_domain_output, t_spatial_output, t_filter_output = my_net(input_data=t_eeg, domain=1, alpha=1)
            # 源域的目标域误差
            err_t_label = loss_class(my_LogSoftmax(t_class_output), t_label.long())
            err_t_domain = loss_domain(my_LogSoftmax(t_domain_output), t_domain_label)


            # 开始计算损失和梯度

            # 计算label classifier loss 的梯度
            err_s_label.backward(retain_graph=True)
            # 由于label classifier loss在 feature extractor有梯度，因此截取label classifier loss在feature extractor的梯度，等待进一步合并计算。
            gradients_l_y_feature_extractor = steal_gradients_feature_extractor(my_net)
            # label classifier loss 不会对 alignment head 直接造成影响，故将其梯度置0
            my_net.alignment_head_source.custom_zero_grad()
            my_net.alignment_head_target.custom_zero_grad()

            #计算domain classifier loss
            l_d = err_t_domain + err_s_domain
            l_d.backward(retain_graph=True)
            # 由于domain classifier loss 在 feature extractor有梯度，因此截取domain classifier loss在feature extractor的梯度，等待进一步合并计算。
            # 截取domain classifier的loss在共同参数上的梯度（共同参数上的梯度被置零）
            gradients_l_d_feature_extractor = steal_gradients_feature_extractor(my_net)
            # 由于domain classifier loss在 target transfer matrix有梯度，因此截取domain classifier loss在target transfer matrix的梯度，等待进一步合并计算。
            # 截取domain classifier的loss在共同参数上的梯度（共同参数上的梯度被置零）
            gradients_l_d = steal_gradients_target_transfer_matrix(my_net)


            # 计算 alignment head 的正则化 loss
            err_s_alignment_head = my_net.alignment_head_source.get_magnitude_loss()
            err_t_alignment_head = my_net.alignment_head_target.get_magnitude_loss()
            err_st_alignment_head = err_s_alignment_head + err_t_alignment_head
            err_st_alignment_head.backward(retain_graph=True)


            # 计算 loss_fre
            loss_fre = F.l1_loss(s_filter_output, t_filter_output, reduction='mean')/1000
            loss_fre.backward(retain_graph=True)
            # 由于loss_fre在 target transfer matrix有梯度，因此截取loss_fre在target transfer matrix的梯度，等待进一步合并计算。
            # 截取loss_fre在共同参数上的梯度（共同参数上的梯度被置零）
            gradients_loss_fre = steal_gradients_target_transfer_matrix(my_net)
            # 计算 cov_loss
            loss_cov = cov_loss_cos_distance(s_spatial_output, t_spatial_output)
            loss_cov.backward(retain_graph=True)
            # 由于loss_cov在 target transfer matrix有梯度，因此截取loss_cov在target transfer matrix的梯度，等待进一步合并计算。
            # 截取loss_cov在共同参数上的梯度（共同参数上的梯度被置零）
            gradients_loss_cov = steal_gradients_target_transfer_matrix(my_net)

            # 计算 target_transfer_matrix的共同参数的梯度，由三部分梯度{"l_d":gradients_l_d, "l_cov":gradients_loss_cov, "l_fre":gradients_loss_fre}求解而得======================
            loss_data = {"l_d":l_d, "l_cov":loss_cov, "l_fre":loss_fre}
            __multi_grads_norm__ = {"l_d":gradients_l_d.copy(), "l_cov":gradients_loss_cov.copy(), "l_fre":gradients_loss_fre.copy()}
            gn = gradient_normalizers(__multi_grads_norm__, loss_data, "loss+")
            for t in ["l_d", "l_cov", "l_fre"]:
                for gr_i in range(len(__multi_grads_norm__[t])):
                    __multi_grads_norm__[t][gr_i] = __multi_grads_norm__[t][gr_i] / gn[t]
            list_multi_grads = [__multi_grads_norm__[t] for t in ["l_d", "l_cov", "l_fre"]]
            sol, min_norm = minnormsolver.find_min_norm_element(list_multi_grads)
            sol = torch.tensor(sol)
            optimal_grad = sol[0]*gradients_l_d[0] + sol[1]*gradients_loss_cov[0] + sol[2]*gradients_loss_fre[0]
            my_net.alignment_head_target.channel_transfer_matrix.grad = optimal_grad
            with torch.no_grad():
                gradient_channel = torch.mean(torch.abs(my_net.alignment_head_target.channel_transfer_matrix.grad.data))
            # ======================

            # 计算 feature_extrator的共同参数的梯度，由三部分梯度{"l_d":gradients_l_d, "l_label":gradients_loss_label}求解而得======================
            loss_data_feature_extractor = {"l_d":l_d, "l_y":err_s_label}
            __multi_grads_norm_feature_extractor__ = {"l_d":gradients_l_d_feature_extractor.copy(), "l_y":gradients_l_y_feature_extractor.copy()}

            gn_feature_extractor= gradient_normalizers(__multi_grads_norm_feature_extractor__, loss_data_feature_extractor, "loss+")
            for t in ["l_d", "l_y"]:
                for gr_i in range(len(__multi_grads_norm_feature_extractor__[t])):
                    __multi_grads_norm_feature_extractor__[t][gr_i] = __multi_grads_norm_feature_extractor__[t][gr_i] / gn_feature_extractor[t]
            list_multi_grads_feature_extractor = [__multi_grads_norm_feature_extractor__[t] for t in ["l_d", "l_y"]]
            sol, min_norm = minnormsolver.find_min_norm_element(list_multi_grads_feature_extractor)
            sol = torch.tensor(sol)
            param_id = 0
            for param in my_net.feature.parameters():
                if param.grad is not None:
                    param.grad = sol[0]*gradients_l_d_feature_extractor[param_id] + sol[1]*gradients_l_y_feature_extractor[param_id]
                    param_id += 1
            # ======================
            with torch.no_grad():
                gradient_channel = torch.mean(torch.abs(my_net.alignment_head_target.channel_transfer_matrix.grad.data))
            # ======================
            # 更新权重
            optimizer.step()
            scheduler.step(epoch * len_dataloader + i)

            if config.getint('debug', 'isdebug'):
                sys.stdout.write(
                    '\r epoch: %d, [iter: %d / all %d], err_s_class: %f, err_t_class:%f, err_s_domain: %f, err_t_domain: %f, loss_fre: %f, loss_cov: %f, gradient_channel: %f' \
                    % (epoch, i + 1, len_dataloader, err_s_label.data.cpu().numpy(), err_t_label.data.cpu().numpy(),
                       err_s_domain.data.cpu().numpy(), err_t_domain.data.cpu().item(), loss_fre.data.cpu().item(), loss_cov.data.cpu().item(), gradient_channel.data.cpu().item()))
                sys.stdout.flush()

            with torch.no_grad():
                writer.add_scalar('err_s_label', err_s_label, epoch * len_dataloader + i)
                writer.add_scalar('err_t_label', err_t_label, epoch * len_dataloader + i)
                writer.add_scalar('err_s_domain', err_s_domain, epoch * len_dataloader + i)
                writer.add_scalar('err_t_domain', err_t_domain, epoch * len_dataloader + i)
                writer.add_scalar('l1_distance', err_t_domain, epoch * len_dataloader + i)
                writer.add_scalar('cov_distance', err_t_domain, epoch * len_dataloader + i)


        print('\n')
        acc_source = test(test_list=source_eval_list, torch_model=my_net, domain=0, num_channel=config.getint('settings', 'source_num_channel'))
        print('Cross: %d, Epoch: %d. Accuracy of the %s validation set: %f' % (cross_id, epoch, "Source", acc_source))

        acc_target = test(test_list=target_eval_list, torch_model=my_net, domain=1, num_channel=config.getint('settings', 'target_num_channel'))
        print('Cross: %d, Epoch: %d. Accuracy of the %s validation set: %f' % (cross_id, epoch, "Target", acc_target))
        writer.add_scalar('Source Validation Set Accuracy', acc_source, epoch)
        writer.add_scalar('Target Validation Set Accuracy', acc_target, epoch)

        if acc_source > best_acc_source:
            best_acc_source = acc_source
            best_index_source = epoch
            torch.save(my_net,
                       os.path.join(model_root, cache_prefix + '_cross_id_{0}_best_source_model.pth'.format(cross_id)))

        if acc_target > best_acc_target:
            best_acc_target = acc_target
            best_index_target = epoch
            torch.save(my_net,
                       os.path.join(model_root, cache_prefix + '_cross_id_{0}_best_target_model.pth'.format(cross_id)))

    print('============ Summary ============= \n')
    print('\n')
    test_acc_source = test(test_list=source_test_list,
                                               torch_model=cache_prefix + '_cross_id_{0}_best_source_model.pth'.format(cross_id), domain=0,
                                               num_channel=config.getint('settings', 'source_num_channel'))
    print('Accuracy of the %s test set: %f' % ("Source", test_acc_source))
    test_acc_target = test(test_list=target_test_list,
                                               torch_model=cache_prefix + '_cross_id_{0}_best_target_model.pth'.format(cross_id), domain=1,
                                               num_channel=config.getint('settings', 'target_num_channel'))
    print('Accuracy of the %s test set: %f' % ("Target", test_acc_target))

    print('Accuracy of the Exp12(Source) validation set: {0} at {1}'.format(best_acc_source, best_index_source))
    print('Accuracy of the Exp3(Target) validation set: {0} at {1}'.format(best_acc_target, best_index_target))
    record_val_metric[0, cross_id] = best_acc_source
    record_val_metric[1, cross_id] = best_acc_target

    record_test_metric[0, cross_id] = test_acc_source
    record_test_metric[1, cross_id] = test_acc_target

np.save(os.path.join("record", cache_prefix + "_val_metric.npy"), record_val_metric)
np.save(os.path.join("record", cache_prefix + "_test_metric.npy"), record_test_metric)
print("============Final Summary==================================")
print("record_val_metric")
print(record_val_metric)
print(np.mean(record_val_metric, axis=1))
print("record_test_metric")
print(record_test_metric)
print(np.mean(record_test_metric, axis=1))
append_results_to_csv(cache_prefix, record_val_metric, record_test_metric, file_path=os.path.join("record", "comparison_study_EEGInfinity004.csv"))

import os
import sys
from torch.optim import SGD
import torch.utils.data
import numpy as np
from my_utils.data_loader_npy import EEGDataSet
from my_utils.model_EEG_Infinity001 import DANN_EEG
from my_utils.test_MengData import test
from my_utils.my_tool import CustomLRScheduler
from my_utils.recorder import append_results_to_csv
from torch.utils.tensorboard import SummaryWriter
import configparser
import argparse

parser = argparse.ArgumentParser(description='Read configuration file.')
parser.add_argument('--config', default='config_PhysioNetMIToMengExp3.ini', help='Path to the config.ini file')
parser.add_argument('--cache_prefix', default='parser_test', help='prefix of the cache (IMPORTANT!)')
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

    transfer_matrix_source = CAR_matrix_source

    CAR_matrix_target = torch.eye(config.getint('settings', 'target_num_channel')) - torch.ones(
        [config.getint('settings', 'target_num_channel'),
         config.getint('settings', 'target_num_channel')]) / config.getint('settings', 'target_num_channel')
    transfer_matrix_target = torch.matmul(
        torch.tensor(np.load(os.path.join('config', config.get('settings', 'file_name_transfer_matrix')))).to(
            torch.float32), CAR_matrix_target)

    my_net = DANN_EEG(transfer_matrix_source, transfer_matrix_target, FIR_order=17)

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
    for p in my_net.parameters():
        p.requires_grad = True

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

            p = float(i + epoch * len_dataloader) / n_epoch / len_dataloader
            # 梯度反转层的lambda因子
            alpha = 2. / (1. + np.exp(config.getint('GRL', 'decay') * p)) - 1

            # 使用源域进行训练
            data_source = next(data_source_iter)

            s_eeg, s_subject, s_label = data_source
            my_net.zero_grad()
            batch_size = len(s_label)
            s_domain_label = torch.ones(batch_size).long()
            if cuda:
                s_eeg = s_eeg.cuda()
                s_label = s_label.cuda()
                s_domain_label = s_domain_label.cuda()

            class_output, domain_output, s_alignment_head_output = my_net(input_data=s_eeg, domain=0, alpha=alpha)
            # 源域的分类误差

            err_s_label = loss_class(my_LogSoftmax(class_output), s_label.long())
            err_s_domain = loss_domain(my_LogSoftmax(domain_output), s_domain_label)

            # 使用目标域进行训练
            data_target = next(data_target_iter)
            t_eeg, t_subject, t_label = data_target

            batch_size = len(t_label)

            t_domain_label = torch.zeros(batch_size).long()

            if cuda:
                t_eeg = t_eeg.cuda()
                t_label = t_label.cuda()
                t_domain_label = t_domain_label.cuda()

            t_class_ouput, t_domain_output, t_alignment_head_output = my_net(input_data=t_eeg, domain=1, alpha=alpha)

            err_t_label = loss_class(my_LogSoftmax(t_class_ouput), t_label.long())
            err_t_domain = loss_domain(my_LogSoftmax(t_domain_output), t_domain_label)

            # 关节角的spatial_loss
            # err_transfer_matrix = custom_ReLU_loss(1 - my_net.get_spatial_loss_angle())
            # 首先反向传播源于classification loss
            err_s_label.backward(retain_graph=True)
            # 清除classification loss 对alignment head的梯度
            my_net.alignment_head_source.custom_zero_grad()
            # 清除 alignment head的梯度之后再反向传播 domain 的loss
            err_s_domain.backward(retain_graph=True)
            err_t_domain.backward(retain_graph=True)
            # err_transfer_matrix.backward(retain_graph=True)
            # 更新权重
            optimizer.step()
            scheduler.step(epoch * len_dataloader + i)
            if config.getint('debug', 'isdebug'):
                sys.stdout.write(
                    '\r epoch: %d, [iter: %d / all %d], err_s_class: %f, err_s_domain: %f, err_t_domain: %f' \
                    % (epoch, i + 1, len_dataloader, err_s_label.data.cpu().numpy(),
                       err_s_domain.data.cpu().numpy(), err_t_domain.data.cpu().item()))
                sys.stdout.flush()

            with torch.no_grad():
                writer.add_scalar('err_s_label', err_s_label, epoch * len_dataloader + i)
                writer.add_scalar('err_t_label', err_t_label, epoch * len_dataloader + i)
                writer.add_scalar('err_s_domain', err_s_domain, epoch * len_dataloader + i)
                writer.add_scalar('err_t_domain', err_t_domain, epoch * len_dataloader + i)
                writer.add_scalar('spatial_loss', my_net.get_spatial_loss(), epoch * len_dataloader + i)
                writer.add_scalar('spatial_loss(angle)', my_net.get_spatial_loss_angle(), epoch * len_dataloader + i)
                writer.add_scalar('s_alignment_head_output_dc', torch.mean(s_alignment_head_output, dim=(0, 1, 2, 3)),
                                  epoch * len_dataloader + i)
                writer.add_scalar('t_alignment_head_output_dc', torch.mean(t_alignment_head_output, dim=(0, 1, 2, 3)),
                                  epoch * len_dataloader + i)
            # torch.save(my_net.alignment_head_source.state_dict(), os.path.join("collect_data",
            #                                                                    cache_suffix + f'{epoch * len_dataloader + i}_{cross_id}_source_alignment_head_parameters.pth'))
            # torch.save(my_net.alignment_head_target.state_dict(), os.path.join("collect_data",
            #                                                                    cache_suffix + f'{epoch * len_dataloader + i}_{cross_id}_target_alignment_head_parameters.pth'))
        torch.save(my_net, model_root + '/' + cache_prefix + '.pth')
        print('\n')
        acc_source, conf_matrix_source = test(test_list=source_eval_list, model_path=cache_prefix + '.pth', domain=0,
                                              num_channel=config.getint('settings', 'source_num_channel'))
        print('Cross: %d, Epoch: %d. Accuracy of the %s validation set: %f' % (cross_id, epoch,"Source", acc_source))

        acc_target, conf_matrix_target = test(test_list=target_eval_list, model_path=cache_prefix + '.pth', domain=1,
                                              num_channel=config.getint('settings', 'target_num_channel'))
        print('Cross: %d, Epoch: %d. Accuracy of the %s validation set: %f' % (cross_id, epoch,"Target", acc_target))
        writer.add_scalar('Source Validation Set Accuracy', acc_source, epoch)
        writer.add_scalar('Target Validation Set Accuracy', acc_target, epoch)

        if acc_source > best_acc_source:
            best_acc_source = acc_source
            best_index_source = epoch
            best_conf_source = conf_matrix_source
            torch.save(my_net,
                       os.path.join(model_root, cache_prefix + '_cross_id_{0}_best_source_model.pth'.format(cross_id)))

        if acc_target > best_acc_target:
            best_acc_target = acc_target
            best_index_target = epoch
            best_conf_target = conf_matrix_target
            torch.save(my_net,
                       os.path.join(model_root, cache_prefix + '_cross_id_{0}_best_target_model.pth'.format(cross_id)))

    print('============ Summary ============= \n')
    print('\n')
    test_acc_source, conf_matrix_source = test(test_list=source_test_list,
                                               model_path=cache_prefix + '_cross_id_{0}_best_source_model.pth'.format(
                                                   cross_id), domain=0,
                                               num_channel=config.getint('settings', 'source_num_channel'))
    print('Accuracy of the %s test set: %f' % ("Source", test_acc_source))
    test_acc_target, conf_matrix_target = test(test_list=target_test_list,
                                               model_path=cache_prefix + '_cross_id_{0}_best_target_model.pth'.format(
                                                   cross_id), domain=1,
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
append_results_to_csv(cache_prefix, record_val_metric, record_test_metric)

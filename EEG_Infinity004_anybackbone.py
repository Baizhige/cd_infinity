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
    Return the loss to the model, capture and save the gradients of specific parameters, and clear the gradients of specified parameters.
    Args:
        loss: The loss for which the gradient is to be calculated.
        model: The network parameters passed in.
    Returns:
        The gradients of the "common parameters."
        Common parameters include:
        -- domain_filter
        -- channel_transfer_matrix
        -- domain_filter
    '''
    gradients = [Variable(model.alignment_head_target.channel_transfer_matrix.grad.data.clone(), requires_grad=False)]
    model.alignment_head_target.channel_transfer_matrix.grad.zero_()
    return gradients

def steal_gradients_feature_extractor(model):
    '''
    Return the loss to the model, capture and save the gradients of all parameters under the feature module, and clear the gradients of these parameters.
    Args:
        loss: The loss for which the gradient is to be calculated.
        model: The network parameters passed in.
    Returns:
        The gradients of all parameters under the feature module.
    '''
    # steal gradients for all weights in feature module
    gradients = []
    for param in model.feature.parameters():
        if param.grad is not None:
            gradients.append(Variable(param.grad.data.clone(), requires_grad=False))
            # zero gradients
            param.grad.zero_()
    return gradients

def cov_loss_cos_distance(tensorA, tensorB):
    """
    Calculate the cosine distance between the average covariance matrices of two tensors (tensorA and tensorB).

    Both tensorA and tensorB are tensors with the shape (batchsize, 1, c, T).
    Each tensor contains batchsize samples, and each sample is a matrix with c rows and T columns.
    """

    def compute_mean_covariance(input_tensor):
        # Remove dimensions of size 1, changing the tensor shape to (batchsize, c, T)
        input_tensor = input_tensor.squeeze(1)

        # Data centralization: subtract the mean from each feature
        mean = input_tensor.mean(dim=-1, keepdim=True)
        input_tensor_centered = input_tensor - mean

        # Calculate the covariance matrix
        covariance_matrices = torch.matmul(input_tensor_centered, input_tensor_centered.transpose(1, 2)) / (
                    input_tensor_centered.shape[-1] - 1)

        # Calculate the mean covariance matrix
        mean_covariance = covariance_matrices.mean(dim=0)
        return mean_covariance

    # Compute the mean covariance matrices for two tensors
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

    # two negative log losses
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
        # for each epoch, do:
        data_source_iter = iter(source_train_dataloader)
        data_target_iter = iter(target_train_dataloader)
        my_net.train()
        for i in range(len_dataloader):
            my_net.zero_grad()
            # forward source data
            data_source = next(data_source_iter)

            s_eeg, s_subject, s_label = data_source

            s_domain_label = torch.ones(len(s_label)).long()
            if cuda:
                s_eeg = s_eeg.cuda()
                s_label = s_label.cuda()
                s_domain_label = s_domain_label.cuda()

            # forward target data
            data_target = next(data_target_iter)
            t_eeg, t_subject, t_label = data_target

            t_domain_label = torch.zeros(len(t_label)).long()

            if cuda:
                t_eeg = t_eeg.cuda()
                t_label = t_label.cuda()
                t_domain_label = t_domain_label.cuda()

            if len(s_label) != len(t_label):
                # In the last iteration, there may be an asymmetry in the number of source and target domains, just pass.
                continue

            s_class_output, s_domain_output, s_spatial_output, s_filter_output = my_net(input_data=s_eeg, domain=0, alpha=1)
            # cls loss for source domain
            err_s_label = loss_class(my_LogSoftmax(s_class_output), s_label.long())
            err_s_domain = loss_domain(my_LogSoftmax(s_domain_output), s_domain_label)


            t_class_output, t_domain_output, t_spatial_output, t_filter_output = my_net(input_data=t_eeg, domain=1, alpha=1)
            # target loss
            err_t_label = loss_class(my_LogSoftmax(t_class_output), t_label.long())
            err_t_domain = loss_domain(my_LogSoftmax(t_domain_output), t_domain_label)


            # begin to calculate loss

            # for label classifier loss gradient
            err_s_label.backward(retain_graph=True)
            # As label classifier loss for feature extractor has gradients, so clip the gradient for label classifier loss in feature extractor, waiting for next combination
            gradients_l_y_feature_extractor = steal_gradients_feature_extractor(my_net)
            # label classifer loss do not contribute to alignment head directly, so set the gradients to zero.
            my_net.alignment_head_source.custom_zero_grad()
            my_net.alignment_head_target.custom_zero_grad()

            # Calculate the domain classifier loss
            l_d = err_t_domain + err_s_domain
            l_d.backward(retain_graph=True)
            # Since the domain classifier loss has gradients in the feature extractor, capture the domain classifier loss gradients in the feature extractor and wait for further combination calculations.
            # Capture the domain classifier loss gradients in the shared parameters (the gradients in the shared parameters are set to zero)
            gradients_l_d_feature_extractor = steal_gradients_feature_extractor(my_net)
            # Since the domain classifier loss has gradients in the target transfer matrix, capture the domain classifier loss gradients in the target transfer matrix and wait for further combination calculations.
            # Capture the domain classifier loss gradients in the shared parameters (the gradients in the shared parameters are set to zero)
            gradients_l_d = steal_gradients_target_transfer_matrix(my_net)

            # Calculate the regularization loss for the alignment head
            err_s_alignment_head = my_net.alignment_head_source.get_magnitude_loss()
            err_t_alignment_head = my_net.alignment_head_target.get_magnitude_loss()
            err_st_alignment_head = err_s_alignment_head + err_t_alignment_head
            err_st_alignment_head.backward(retain_graph=True)

            # Calculate the loss_fre
            loss_fre = F.l1_loss(s_filter_output, t_filter_output, reduction='mean')/1000
            loss_fre.backward(retain_graph=True)
            # Since loss_fre has gradients in the target transfer matrix, capture the loss_fre gradients in the target transfer matrix and wait for further combination calculations.
            # Capture the loss_fre gradients in the shared parameters (the gradients in the shared parameters are set to zero)
            gradients_loss_fre = steal_gradients_target_transfer_matrix(my_net)
            # Calculate cov_loss
            loss_cov = cov_loss_cos_distance(s_spatial_output, t_spatial_output)
            loss_cov.backward(retain_graph=True)
            # Since loss_cov has gradients in the target transfer matrix, capture the loss_cov gradients in the target transfer matrix and wait for further combination calculations.
            # Capture the loss_cov gradients in the shared parameters (the gradients in the shared parameters are set to zero)
            gradients_loss_cov = steal_gradients_target_transfer_matrix(my_net)

            # Calculate the gradients of the shared parameters of the target_transfer_matrix, obtained by solving the three parts of the gradients {"l_d":gradients_l_d, "l_cov":gradients_loss_cov, "l_fre":gradients_loss_fre}
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

            # Calculate the gradients of the shared parameters of the feature_extractor, obtained by solving the two parts of the gradients {"l_d":gradients_l_d, "l_label":gradients_loss_label}
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
            # Update weights
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

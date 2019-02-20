import os
os.environ['CUDA_VISIBLE_DEVICES'] =  '5' #'3,2,1,0'
import sys
sys.path.append("..")
import argparse
from process.data_fusion import *
from process.augmentation import *
from net.rate import *
from file import *
from metric import *

def get_model(model_name, num_class, is_first_bn):

    if model_name == 'resnet18':
        from model_fusion.model_resnet18_SEFusion import FusionNet
    elif model_name == 'resnet18_cut':
        from model_fusion.model_resnet18_SEFusion_cut import FusionNet
    elif model_name == 'resnet34':
        from model_fusion.model_resnet34_SEFusion import FusionNet
    elif model_name == 'resnet18_halfway':
        from model_fusion.model_resnet18_HalfwayFusion import FusionNet
    elif model_name == 'resnet18_dcn_v2':
        from model_fusion.model_DCN_v2_resnet18_SEFusion import FusionNet
    elif model_name == 'resnet18_final':
        from model_fusion.model_resnet18_FinalFusion import FusionNet
    elif model_name == 'resnet_tiny':
        from model_fusion.model_resnet_tiny_SEFusion import FusionNet
    elif model_name == 'resnet18_scse':
        from model_fusion.model_resnet18_SCSEFusion import FusionNet
    elif model_name == 'resnet18_scseV2':
        from model_fusion.model_resnet18_SCSEFusionV2 import FusionNet
    elif model_name == 'resnet18_scseV3':
        from model_fusion.model_resnet18_SCSEFusionV3 import FusionNet

    net = FusionNet(num_class=num_class)
    return net

def get_augment(image_mode):
    if image_mode == 'color':
        augment = color_augumentor
    elif image_mode == 'depth':
        augment = depth_augumentor
    elif image_mode == 'ir':
        augment = ir_augumentor
    return augment

def run_train(config):
    out_dir = './models'
    out_dir = os.path.join(out_dir,config.model_name)
    initial_checkpoint = config.pretrained_model

    schduler = DecayScheduler(base_lr=0.1, decay=0.1, step=10) #it means 3 epoch for 0.01, 3 epoch for 0.001
    criterion          = softmax_cross_entropy_criterion

    ## setup  -----------------------------------------------------------------------------
    if not os.path.exists(out_dir +'/checkpoint'):
        os.makedirs(out_dir +'/checkpoint')
    if not os.path.exists(out_dir +'/backup'):
        os.makedirs(out_dir +'/backup')
    if not os.path.exists(out_dir +'/backup'):
        os.makedirs(out_dir +'/backup')

    log = Logger()
    log.open(os.path.join(out_dir,config.model_name+'.txt'),mode='a')
    log.write('\tout_dir      = %s\n' % out_dir)
    log.write('\n')
    log.write('\t<additional comments>\n')
    log.write('\t  ... xxx baseline  ... \n')
    log.write('\n')

    ## dataset ----------------------------------------
    log.write('** dataset setting **\n')

    augment = None
    train_dataset = FDDataset(mode = 'train', modality=config.image_mode,image_size=config.image_size,
                              fold_index=config.train_fold_index,augment=augment)
    train_loader  = DataLoader(train_dataset,
                                shuffle=True,
                                batch_size  = config.batch_size,
                                drop_last   = True,
                                num_workers = 4)

    valid_dataset = FDDataset(mode = 'test', modality=config.image_mode,image_size=config.image_size,
                              fold_index=config.train_fold_index,augment=augment)
    valid_loader  = DataLoader( valid_dataset,
                                shuffle=False,
                                batch_size  = config.batch_size,
                                drop_last   = False,
                                num_workers = 4)

    assert(len(train_dataset)>=config.batch_size)
    log.write('batch_size = %d\n'%(config.batch_size))
    log.write('train_dataset : \n%s\n'%(train_dataset))
    log.write('valid_dataset : \n%s\n'%(valid_dataset))
    log.write('\n')

    ## net ----------------------------------------
    log.write('** net setting **\n')

    net = get_model(model_name=config.model, num_class=2,is_first_bn=True)
    print(net)
    net = torch.nn.DataParallel(net)
    net =  net.cuda()

    if initial_checkpoint is not None:
        initial_checkpoint = os.path.join(out_dir +'/checkpoint',initial_checkpoint)
        print('\tinitial_checkpoint = %s\n' % initial_checkpoint)
        net.load_state_dict(torch.load(initial_checkpoint, map_location=lambda storage, loc: storage))

    log.write('%s\n'%(type(net)))
    log.write('criterion=%s\n'%criterion)
    log.write('\n')

    ## optimiser ----------------------------------
    if 0: ##freeze
        for p in net.resnet.parameters(): p.requires_grad = False
        for p in net.encoder1.parameters(): p.requires_grad = False
        for p in net.encoder2.parameters(): p.requires_grad = False
        for p in net.encoder3.parameters(): p.requires_grad = False
        for p in net.encoder4.parameters(): p.requires_grad = False
        pass

    #-----------------------------------------------
    optimizer = optim.SGD(filter(lambda p: p.requires_grad, net.parameters()),
                          lr=schduler.get_rate(0), momentum=0.9, weight_decay=0.0005)

    iter_smooth = 20
    start_iter = 0

    log.write('schduler\n  %s\n'%(schduler))
    log.write('\n')

    ## start training here! ##############################################
    log.write('** start training here! **\n')
    log.write('                    |------------ VALID -------------|-------- TRAIN/BATCH ----------|         \n')
    log.write('rate   iter  epoch  | loss   acc-1  acc-3   lb       | loss   acc-1  acc-3   lb      |  time   \n')
    log.write('----------------------------------------------------------------------------------------------------\n')

    train_loss   = np.zeros(6,np.float32)
    valid_loss   = np.zeros(6,np.float32)
    batch_loss   = np.zeros(6,np.float32)
    iter = 0
    i    = 0

    start = timer()
    train_epoch = 30

    min_acer = 1.0
    for epoch in range(train_epoch):
        sum_train_loss = np.zeros(6,np.float32)
        sum = 0
        optimizer.zero_grad()

        for input, truth in train_loader:
            iter = i + start_iter

            # learning rate schduler -------------
            lr = schduler.get_rate(epoch)
            if lr<0 : break
            adjust_learning_rate(optimizer, lr)
            rate = get_learning_rate(optimizer)

            # one iteration update  -------------
            net.train()
            input = input.cuda()
            truth = truth.cuda()

            logit,_,_ = net.forward(input)
            truth = truth.view(logit.shape[0])

            loss  = criterion(logit, truth)
            precision,_ = metric(logit, truth)

            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            # print statistics  ------------
            batch_loss[:2] = np.array(( loss.item(), precision.item(),))

            sum += 1
            if iter%iter_smooth == 0:
                train_loss = sum_train_loss/sum
                sum = 0

            if i%50==0:
                net.eval()
                valid_loss,_ = do_valid_test(net, valid_loader, criterion)
                net.train()

                asterisk = ' '
                log.write(config.model_name+' %0.4f %5.1f %6.1f | %0.6f  %0.6f  %0.3f  (%0.3f)%s  | %0.6f  %0.6f  %0.3f  (%0.3f) |%s \n' % (
                         rate, iter, epoch,
                         valid_loss[0], valid_loss[1], valid_loss[2], valid_loss[3],asterisk,
                         batch_loss[0], 0.0, 0.0, batch_loss[1],
                         time_to_str((timer() - start), 'min')))

                print(config.model_name+' %0.4f %5.1f %6.1f | %0.6f  %0.6f  %0.3f  (%0.3f)%s  | %0.6f  %0.6f  %0.3f  (%0.3f)  | %s' % (
                             rate, iter, epoch,
                             valid_loss[0], valid_loss[1], valid_loss[2], valid_loss[3],' ',
                             batch_loss[0], 0.0, 0.0, batch_loss[1],
                             time_to_str((timer() - start),'min')))

                if valid_loss[1] < min_acer:
                    min_acer = valid_loss[1]

                    torch.save(net.state_dict(), out_dir + '/checkpoint/min_acer_model.pth')
                    log.write('save min acer model: ' + str(min_acer) +'\n')

            i=i+1

    torch.save(net.state_dict(), out_dir + '/checkpoint/final_model.pth')
    log.write('save final model')

def run_valid(config):
    out_dir = './models'
    out_dir = os.path.join(out_dir,config.model_name)
    initial_checkpoint = config.pretrained_model

    valid_dataset = FDDataset(mode = 'val', modality='color',image_size=config.image_size,fold_index=config.train_fold_index)
    valid_loader  = DataLoader( valid_dataset,
                                shuffle=False,
                                batch_size  = config.batch_size,
                                drop_last   = False,
                                num_workers=8)

    ## net ----------------------------------------
    net = get_model(model_name=config.model, num_class=2,is_first_bn=True)
    print(net)
    net = torch.nn.DataParallel(net)
    net =  net.cuda()

    if initial_checkpoint is not None:
        initial_checkpoint = os.path.join(out_dir +'/checkpoint',initial_checkpoint)
        print('\tinitial_checkpoint = %s\n' % initial_checkpoint)
        net.load_state_dict(torch.load(initial_checkpoint, map_location=lambda storage, loc: storage))

    criterion = softmax_cross_entropy_criterion

    net.eval()
    valid_loss = do_valid_test(net, valid_loader, criterion)
    net.train()

    print('%0.6f  %0.6f  %0.3f  (%0.3f) \n' % (valid_loss[0], valid_loss[1], valid_loss[2], valid_loss[3]))

def run_valid_test(config):
    out_dir = './models'
    out_dir = os.path.join(out_dir,config.model_name)
    initial_checkpoint = config.pretrained_model

    valid_dataset = FDDataset(mode = 'test', modality='color',image_size=config.image_size,fold_index=config.train_fold_index)
    valid_loader  = DataLoader( valid_dataset,
                                shuffle=False,
                                batch_size  = config.batch_size,
                                drop_last   = False,
                                num_workers=8)
    ## net ----------------------------------------
    net = get_model(model_name=config.model, num_class=2,is_first_bn=True)
    print(net)
    net = torch.nn.DataParallel(net)
    net =  net.cuda()

    if initial_checkpoint is not None:
        initial_checkpoint = os.path.join(out_dir +'/checkpoint',initial_checkpoint)
        print('\tinitial_checkpoint = %s\n' % initial_checkpoint)
        net.load_state_dict(torch.load(initial_checkpoint, map_location=lambda storage, loc: storage))

    criterion = softmax_cross_entropy_criterion
    net.eval()
    valid_loss = do_valid_test(net, valid_loader, criterion)
    net.train()

    print('%0.6f  %0.6f  %0.3f  (%0.3f) \n' % (valid_loss[0], valid_loss[1], valid_loss[2], valid_loss[3]))

def run_valid_test_10crop(config):
    out_dir = './models'
    out_dir = os.path.join(out_dir,config.model_name)
    initial_checkpoint = config.pretrained_model

    ## net ----------------------------------------
    net = get_model(model_name=config.model, num_class=2,is_first_bn=True)
    print(net)
    net = torch.nn.DataParallel(net)
    net =  net.cuda()

    if initial_checkpoint is not None:
        initial_checkpoint = os.path.join(out_dir +'/checkpoint',initial_checkpoint)
        print('\tinitial_checkpoint = %s\n' % initial_checkpoint)
        net.load_state_dict(torch.load(initial_checkpoint, map_location=lambda storage, loc: storage))


    augments = []
    percent_crop = 0.6
    for flip_prob in [0, 1]:
        for top in [0,percent_crop/2.0, percent_crop]:
            for right in [0,percent_crop/2.0, percent_crop]:
                augments.append([flip_prob, top, right, percent_crop - top, percent_crop - right])
        augments.append([flip_prob, percent_crop/2.0, percent_crop/2.0, percent_crop/2.0, percent_crop/2.0])
    print(augments)

    # augments.append([0, 0.1, 0.1, 0.1, 0.1])
    # augments.append([1, 0.1, 0.1, 0.1, 0.1])
    # augments.append([0, 0.3,0.3,0.3,0.3])
    # augments.append([1, 0,0,0,0])

    out_list = []

    for index in range(len(augments)):
        print(augments[index])
        valid_dataset = FDDataset(mode = 'test', modality='color',image_size=config.image_size,
                                  fold_index=config.train_fold_index,augment=augments[index])
        valid_loader  = DataLoader( valid_dataset,
                                    shuffle=False,
                                    batch_size  = config.batch_size,
                                    drop_last   = False,
                                    num_workers=8)

        criterion = softmax_cross_entropy_criterion
        net.eval()
        valid_loss,out = do_valid_test(net, valid_loader, criterion)
        net.train()
        print('%0.6f  %0.6f  %0.3f  (%0.3f) \n' % (valid_loss[0], valid_loss[1], valid_loss[2], valid_loss[3]))
        out_list.append(out)

    probs_tmp = np.zeros(out_list[0][0].shape)

    for prob,label in out_list:
        probs_tmp += prob / (len(out_list)*1.0)

    tpr, fpr, acc = calculate_accuracy(0.5, probs_tmp, label)
    acer, tp, fp, tn, fn = ACER(0.5, probs_tmp, label)

    print('ensemble')
    print(acer)
    print(acc)
    print('tp: '+str(tp))
    print('tn: '+str(tn))
    print('fp: '+str(fp))
    print('fn: '+str(fn))

    submission(probs_tmp,initial_checkpoint+str(acer)[0:6]+'.txt')

    acer_min = 1.0
    thres_min = 0.0
    re = []

    for thres in np.arange(0.0, 1.0, 0.01):
        acer, tp, fp, tn, fn= ACER(thres, probs_tmp, label)
        if acer < acer_min:
            acer_min = acer
            thres_min = thres
            re = [tp, fp, tn, fn]

    print('\n ajust threshold')
    print(acer_min)
    print(thres_min)

    tp, fp, tn, fn = re
    print('tp: '+str(tp))
    print('tn: '+str(tn))
    print('fp: '+str(fp))
    print('fn: '+str(fn))

    try:
        TPR_FPR(probs_tmp, label, fpr_target = 0.01)
        TPR_FPR(probs_tmp, label, fpr_target = 0.001)
        TPR_FPR(probs_tmp, label, fpr_target = 0.0001)
    except:
        return

def run_valid_test_centercrop(config):
    out_dir = './models'
    out_dir = os.path.join(out_dir,config.model_name)
    initial_checkpoint = config.pretrained_model

    ## net ----------------------------------------
    net = get_model(model_name=config.model, num_class=2,is_first_bn=True)
    print(net)
    net = torch.nn.DataParallel(net)
    net =  net.cuda()

    if initial_checkpoint is not None:
        initial_checkpoint = os.path.join(out_dir +'/checkpoint',initial_checkpoint)
        print('\tinitial_checkpoint = %s\n' % initial_checkpoint)
        net.load_state_dict(torch.load(initial_checkpoint, map_location=lambda storage, loc: storage))


    augments = []
    # percent_crop = 0.6
    # for flip_prob in [0, 1]:
    #     for top in [0,percent_crop/2.0, percent_crop]:
    #         for right in [0,percent_crop/2.0, percent_crop]:
    #             augments.append([flip_prob, top, right, percent_crop - top, percent_crop - right])
    #     augments.append([flip_prob, percent_crop/2.0, percent_crop/2.0, percent_crop/2.0, percent_crop/2.0])
    # print(augments)

    # augments.append([0, 0.1, 0.1, 0.1, 0.1])
    # augments.append([1, 0.1, 0.1, 0.1, 0.1])
    augments.append([0, 0.3,0.3,0.3,0.3])
    # augments.append([1, 0,0,0,0])

    out_list = []

    for index in range(len(augments)):
        print(augments[index])
        valid_dataset = FDDataset(mode = 'test', modality='color',image_size=config.image_size,
                                  fold_index=config.train_fold_index,augment=augments[index])
        valid_loader  = DataLoader( valid_dataset,
                                    shuffle=False,
                                    batch_size  = config.batch_size,
                                    drop_last   = False,
                                    num_workers=8)

        criterion = softmax_cross_entropy_criterion
        net.eval()
        valid_loss,out = do_valid_test(net, valid_loader, criterion)
        net.train()
        print('%0.6f  %0.6f  %0.3f  (%0.3f) \n' % (valid_loss[0], valid_loss[1], valid_loss[2], valid_loss[3]))
        out_list.append(out)

    probs_tmp = np.zeros(out_list[0][0].shape)

    for prob,label in out_list:
        probs_tmp += prob / (len(out_list)*1.0)

    tpr, fpr, acc = calculate_accuracy(0.5, probs_tmp, label)
    acer, tp, fp, tn, fn = ACER(0.5, probs_tmp, label)

    print('ensemble')
    print(acer)
    print(acc)
    print('tp: '+str(tp))
    print('tn: '+str(tn))
    print('fp: '+str(fp))
    print('fn: '+str(fn))

    submission(probs_tmp,initial_checkpoint+str(acer)[0:6]+'_noTTA.txt')

    acer_min = 1.0
    thres_min = 0.0
    re = []

    for thres in np.arange(0.0, 1.0, 0.01):
        acer, tp, fp, tn, fn= ACER(thres, probs_tmp, label)
        if acer < acer_min:
            acer_min = acer
            thres_min = thres
            re = [tp, fp, tn, fn]

    print('\n ajust threshold')
    print(acer_min)
    print(thres_min)

    tp, fp, tn, fn = re
    print('tp: '+str(tp))
    print('tn: '+str(tn))
    print('fp: '+str(fp))
    print('fn: '+str(fn))

    try:
        TPR_FPR(probs_tmp, label, fpr_target = 0.01)
        TPR_FPR(probs_tmp, label, fpr_target = 0.001)
        TPR_FPR(probs_tmp, label, fpr_target = 0.0001)
    except:
        return

def run_infer(config):
    out_dir = './models'
    out_dir = os.path.join(out_dir, config.model_name)
    initial_checkpoint = config.pretrained_model

    valid_dataset = FDDataset(mode='val', modality='color', image_size=config.image_size,
                              fold_index=config.train_fold_index)

    valid_loader = DataLoader(valid_dataset,
                              shuffle=False,
                              batch_size=config.batch_size,
                              drop_last=False,
                              num_workers=8)

    ## net ----------------------------------------
    net = get_model(model_name=config.model, num_class=2,is_first_bn=True)
    print(net)
    net = torch.nn.DataParallel(net)
    net = net.cuda()

    if initial_checkpoint is not None:
        initial_checkpoint = os.path.join(out_dir + '/checkpoint', initial_checkpoint)
        print('\tinitial_checkpoint = %s\n' % initial_checkpoint)
        net.load_state_dict(torch.load(initial_checkpoint, map_location=lambda storage, loc: storage))

    criterion = softmax_cross_entropy_criterion

    net.eval()
    test_dataset = FDDataset(mode='test', modality='color', image_size=config.image_size,
                              fold_index=config.train_fold_index)
    test_loader = DataLoader(test_dataset,
                              shuffle=False,
                              batch_size=config.batch_size,
                              drop_last=False,
                              num_workers=8)

    out_file = open('r18_val0.458.txt','w')

    for input, label, id in test_loader:
        batch_size = len(id)

        input = input.cuda()
        logit = net.forward(input)
        prob = F.softmax(logit, 1)
        prob = prob.data.cpu().numpy()

        for i in range(batch_size):
            id_tmp = id[i]
            prob_tmp = prob[i][1]

            out = id_tmp + ' ' + str(prob_tmp)
            print(out)
            out_file.write(out+'\n')

    out_file.close()
    print('done')

def main(config):

    if config.mode == 'train':
        run_train(config)

    if config.mode == 'test':
        run_train(config)

    if config.mode == 'valid_10crop':
        run_valid_test_10crop(config)

    if config.mode == 'valid_centercrop':
        run_valid_test_centercrop(config)

    return

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train_fold_index', type=int, default = -1)
    parser.add_argument('--model', type=str, default='resnet18')

    parser.add_argument('--image_mode', type=str, default='fusion')
    parser.add_argument('--model_name', type=str, default='r18_pretrain_112to48_fold-1_fusion_RC0.6_add_flipUD_t4')

    parser.add_argument('--image_size', type=int, default=48)
    parser.add_argument('--batch_size', type=int, default=512)

    parser.add_argument('--mode', type=str, default='train', choices=['train','test','valid_10crop'])
    parser.add_argument('--pretrained_model', type=str, default=None)

    # parser.add_argument('--mode', type=str, default='valid_10crop', choices=['train','test','valid_10crop'])
    # parser.add_argument('--pretrained_model', type=str, default='final_model.pth')

    parser.add_argument('--iter_save_interval', type=int, default= 1000 * 2)
    parser.add_argument('--iter_valid', type=int, default=100)
    parser.add_argument('--num_iters', type=int, default=200 * 1000 *2)
    parser.add_argument('--step', type=int, default=50 * 1000)

    config = parser.parse_args()
    print(config)
    main(config)
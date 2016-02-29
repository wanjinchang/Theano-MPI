# Server and worker process for asynchronous parallel training
from mpi4py import MPI
from server import Server
from client import Client
import time

def test_intercomm(intercomm,rank):
    
    if intercomm != MPI.COMM_NULL:
        assert intercomm.remote_size == 1
        assert intercomm.size == 1
        assert intercomm.rank ==  0

        if rank == 0: # server
            message = 'from_server'
            root = MPI.ROOT
        else: # worker
            message = None
            root = 0
        message = intercomm.bcast(message, root)
        if rank == 0:
            assert message == None
        else:
            assert message == 'from_server'


class PTBase(object):
    
    '''
    Base class for Parallel Training framework
    Common routine that every device process should excute first
    
    '''
    
    def __init__(self, config, device):
        
    	self.comm = MPI.COMM_WORLD
    	self.rank = self.comm.rank
        self.size = self.comm.size
        self.config = config
        self.device = device
        if self.config['sync_rule'] == 'EASGD':
            self.verbose = True 
        elif self.config['sync_rule'] == 'BSP':
            self.verbose = self.rank==0

        self.process_config()
        self.get_data()
        self.init_device()
        self.build_model()
        
    def process_config(self):
    
        '''
        load some config items
    
        '''
        
        # Add some items in 
        self.config['comm'] = self.comm
        self.config['rank'] = self.rank
        self.config['size'] = self.size
        #self.config['syncrule'] = self.syncrule #TODO add syncrule into config
        self.config['device'] = self.device
        self.config['sock_data'] += int(self.device[-1]) #int(os.environ['OMPI_COMM_WORLD_LOCAL_RANK'])
        self.config['verbose'] = self.verbose
        
        self.model_name=self.config['name']
        import yaml
        with open(self.model_name+'.yaml', 'r') as f:
            model_config = yaml.load(f)
        self.config = dict(self.config.items()+model_config.items())
        
        date = '-%d-%d' % (time.gmtime()[1],time.gmtime()[2])    
        import socket
        self.config['weights_dir']+= '-'+self.config['name'] \
                                     + '-'+str(self.config['size'])+'gpu-' \
                                     + str(self.config['batch_size'])+'b-' \
                                     + socket.gethostname() + date + '/'
                                     
        if self.rank == 0:
            import os
            if not os.path.exists(self.config['weights_dir']):
                os.makedirs(self.config['weights_dir'])
                if self.verbose: print "Creat folder: " + \
                                 self.config['weights_dir']
            else:
                if self.verbose: print "folder exists: " + \
                                 self.config['weights_dir']
            if not os.path.exists(self.config['record_dir']):
                os.makedirs(self.config['record_dir'])
                if self.verbose: print "Creat folder: " + \
                                 self.config['record_dir'] 
            else:
                if self.verbose: print "folder exists: " + \
                                 self.config['record_dir']
                             
        if self.verbose: print self.config
        
    def get_data(self):

        '''
        prepare filename and label list 

        '''
        from helper_funcs import unpack_configs, extend_data
        (flag_para_load, flag_top_5, train_filenames, val_filenames, \
        train_labels, val_labels, img_mean) = unpack_configs(self.config)

        if self.config['debug']:
            train_filenames = train_filenames[:16]
            val_filenames = val_filenames[:8]

        env_train=None
        env_val = None
            
        train_filenames,train_labels,train_lmdb_cur_list,n_train_files=\
            extend_data(self.config,train_filenames,train_labels,env_train)
        val_filenames,val_labels,val_lmdb_cur_list,n_val_files \
    		    = extend_data(self.config,val_filenames,val_labels,env_val)  
        if self.config['data_source'] == 'hkl':
            self.data = [train_filenames,train_labels,\
                        val_filenames,val_labels,img_mean] # 5 items
        else:
            raise NotImplementedError('wrong data source')
    
        if self.verbose: print 'train on %d files' % n_train_files  
        if self.verbose: print 'val on %d files' % n_val_files
    
    def init_device(self):
    
        gpuid = int(self.device[-1])

        # pycuda and zmq set up
        import pycuda.driver as drv

        drv.init()
        dev = drv.Device(gpuid)
        ctx = dev.make_context()
        
        self.drv = drv
        self.dev = dev
        self.ctx = ctx
    
        import theano.sandbox.cuda
        theano.sandbox.cuda.use(self.config['device'])
        
    def build_model(self):

        import theano
        theano.config.on_unused_input = 'warn'

        if self.model_name=='googlenet':
        	from models.googlenet import GoogLeNet
        	#from lib.googlenet import Dropout as drp
        	self.model = GoogLeNet(self.config)

        elif self.model_name=='alexnet':
        	from models.alex_net import AlexNet
        	#from lib.layers import DropoutLayer as drp
        	self.model = AlexNet(self.config)
        else:
            raise NotImplementedError("wrong model name")
        
        
class PTServer(Server, PTBase):
    '''
    Genearl Server class in Parallel Training framework
    
    '''
    
    def __init__(self, port, config, device):
        Server.__init__(self,port=port)
        PTBase.__init__(self,config=config,device=device)
        
        self.worker_comm = {}
        
    def process_request(self, worker_id, message):
        
        # override Server class method 
        reply = None
        
        if message == 'address':
            
            reply = self.address
            print 'sending address to worker', worker_id
        
        return reply
        
    def action_after(self, worker_id, message):
        
        # override Server class method 
        
        if message == 'connect':
            
            client = self.server.accept()[0]
            assert client != None
            fd = client.fileno()
            intercomm = MPI.Comm.Join(fd)
            self.worker_comm[str(worker_id)] = intercomm
            client.close()
            test_intercomm(intercomm, rank=0)
                             
            reply = 'connected'
            print 'connected to worker', worker_id
        


    
class PTWorker(Client, PTBase):
    
    '''
    General Worker class in Parallel Training framework
    
    '''
    
    def __init__(self, port, config, device):
        Client.__init__(self, port = port)
        PTBase.__init__(self, config = config, device = device)
        
        self.config['worker_id'] = self.worker_id
        self.compile_model()  # needs compile model before para_load_init

        if self.config['para_load'] == True:
            self.spawn_load()
            self.para_load_init()
           
    def spawn_load(self):
        
        'parallel loading process'
    
        num_spawn = 1
        hostname = MPI.Get_processor_name()
        mpiinfo = MPI.Info.Create()
        mpiinfo.Set(key = 'host',value = hostname)
        ninfo = mpiinfo.Get_nkeys()
        print ninfo
        import sys
        mpicommand = sys.executable

        gpuid = self.device[-1] #str(os.environ['OMPI_COMM_WORLD_LOCAL_RANK'])
        print gpuid
        socketnum = 0
        
        # adjust numactl according to the layout of copper nodes [1-8]
        if int(gpuid) > 3:
            socketnum=1 
        printstr = "rank" + str(self.rank) +":numa"+ str(socketnum)
        print printstr

        # spawn loading process
        self.icomm= MPI.COMM_SELF.Spawn('numactl', \
                args=['-N',str(socketnum),mpicommand,\
                        '../lib/base/proc_load_mpi.py',gpuid],\
                info = mpiinfo, maxprocs = num_spawn)
        self.config['icomm'] = self.icomm
                
    def para_load_init(self):
        
        # 0. send config dict (can't carry any special objects) to loading process
        
        self.icomm.isend(self.config,dest=0,tag=99)
    	
        drv = self.drv
        shared_x = self.model.shared_x
        img_mean = self.data[4]

        sock_data = self.config['sock_data']
        
        import zmq
        sock = zmq.Context().socket(zmq.PAIR)
        sock.connect('tcp://localhost:{0}'.format(sock_data))
        
        #import theano.sandbox.cuda
        #theano.sandbox.cuda.use(config.device)
        import theano.misc.pycuda_init
        import theano.misc.pycuda_utils
        # pass ipc handle and related information
        gpuarray_batch = theano.misc.pycuda_utils.to_gpuarray(
            shared_x.container.value)
        h = drv.mem_get_ipc_handle(gpuarray_batch.ptr)
        # 1. send ipc handle of shared_x
        sock.send_pyobj((gpuarray_batch.shape, gpuarray_batch.dtype, h))

        # 2. send img_mean
        self.icomm.send(img_mean, dest=0, tag=66)
    
    def para_load_close(self):
        
        # send an stop mode
        self.icomm.send("stop",dest=0,tag=43)
        self.icomm.Disconnect()
        
    def compile_model(self):
        
        from models.googlenet import updates_dict

        compile_time = time.time()
        self.model.compile_train(self.config, updates_dict)
        self.model.compile_val()
        if self.verbose: print 'compile_time %.2f s' % \
                                (time.time() - compile_time)
                                
    def run(self):
        
        # override Client class method
        
        print 'worker started'
        
        self.para_load_close()
        

if __name__ == '__main__':
    
    with open('config.yaml', 'r') as f:
        config = yaml.load(f)
        
    #device = 'gpu' + str(os.environ['OMPI_COMM_WORLD_LOCAL_RANK'])
        
    server = PTServer(port=5555, config=config, device='gpu7')
    
    server.run()
        
    
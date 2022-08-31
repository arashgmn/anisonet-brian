#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This is the main module to set up and simulate an anisotropic network. Other 
modules are auxillary utilities used here. 
"""

import os
import pickle
import numpy as np
import matplotlib.pyplot as plt
import brian2 as b2
from scipy import sparse

from pdb import set_trace
import time

import viz
import utils 
import configs # default configurations
import equations as eq
from landscape import make_landscape
from anisofy import get_post_syns


root = './'

class Simulate(object):
    """
    High-level object for simulating an anisotropic network in Brian.
    """
    
    def __init__(self, net_name='I_net', load_connectivity=True,  scalar=1):
        """
        Initializes the simulator object for the given network configuration. 
        By default, tries to load the connectivity matrix from disk, otherwise
        will generate and save it. Also, prepares the net for a warmup phase 
        that can be executed via the ``warmup`` method.
        
        Upon initialization, a name is attributed to the object which is being
        used for storing figures, connectivity matrices, and states. The name
        has the following strucutre:
            
            ``<anisotropy_type>_<profile_type>_<scaling_factor>``
            
            
        :param net_name: network configuration name, either 'IE_net' or 'I_net'
            , defaults to 'I_net'
        :type net_name: str, optional
        :param scalar: A scaling factor for downsizing the network, defaults to 1
        :type scalar: int, optional    
        """
        
        # making necessary folders
        if not os.path.exists('results'):
            os.makedirs('results')
        if not os.path.exists('results/data'):
            os.makedirs('results/data')
        
        
        # initialize with defaults
        self.pops_cfg , self.conn_cfg = configs.get_config(net_name, scalar=scalar)
        self.base = self.get_synaptic_base()
        self.load_connectivity = load_connectivity
        
        # making a unique name
        self.name = list(self.conn_cfg.values())[0]['anisotropy']['type']
        self.name+= '_'+list(self.conn_cfg.values())[0]['profile']['type'] 
        self.name+= '_'+ str(scalar)
        
        
        self.warmup_std = 500*b2.pA
        self.warmup_dur = 500*b2.ms
        # self.pops_cfg = deepcopy(configs.pops_cfg0)
        # self.lscp_cfg = deepcopy(configs.lscp_cfg0)
        # self.conn_cfg = deepcopy(configs.conn_cfg0)
        # self.syns_cfg = deepcopy(configs.syns_cfg0)
        
        # if pops_cfg!=None:
        #     self.pops_cfg= pops_cfg
        # if lscp_cfg!=None:
        #     self.lscp_cfg= lscp_cfg
        # if conn_cfg!=None:
        #     self.conn_cfg= conn_cfg
        # if syns_cfg!=None:
        #     self.syns_cfg= syns_cfg

    def get_synaptic_base(self):
        """
        Synaptic inputs can be defined in different ways. This method reads the
        configuration keys and determines the used approach. It is important 
        when we define the governing equations.
        """
        
        syn_type = list(self.conn_cfg.values())[0]['synapse']['type']
        
        if 'current' in syn_type:
            return 'current'            
        elif 'voltage' in syn_type:
            return 'voltage'
        elif 'conductance' in syn_type: 
            return 'conductance'
        else:
            raise
        
    def setup_net(self):
        """
        Sets up a network by the following steps:
            #. defining the populations (``setup_pops``)
            #. defining the landscape (``setup_landscape``)
            #. defining the synapses (``setup_syns``)
            #. configuring the spike monintors (``configure_monitors``)
        
        and adds them all to a Brian network object for simulation. Read their 
        description on each method.
        """
        b2.start_scope()
        
        print('Net setup started.')
        self.setup_pops()
        
        print('Landscape setup started.')
        self.setup_landscape()
        
        print('Connecting populations ...')
        self.setup_syns()
        
        print('Configuring spike monitors.')
        self.configure_monitors()
        
        self.net = b2.Network()
        self.net.add(self.pops.values())
        self.net.add(self.syns)
        self.net.add(self.mons)
        print('Net set up.')
        
    
    def setup_pops(self):
        """
        Each population is set up from the ``pops_cfg`` which is a nested 
        dictionary. Look at ``configs`` module for further details. Neurons are
        endowed with a ``coord`` attribute that encodes their ``(x,y)`` index
        on the grid. All neurons are driven by a Gaussian background noise.
        
        Populations are accessible via the `pops` attribute of the ``Simulate``
        object, in form of a dictionary keyed by the population's name.
        
        .. note:
            First index (columns) indicate the `x`-coordinate and second one 
            (rows) the `y`-coordinate. It differs from array convention but 
            mathes the one of images.
        
        """
        self.pops = {}
        for pop_name in self.pops_cfg.keys():
            gs = self.pops_cfg[pop_name]['gs'] # grid size
            cell_cfg = self.pops_cfg[pop_name]['cell']
            noise_cfg = self.pops_cfg[pop_name]['noise']
            
            # initialize population
            eqs = eq.get_nrn_eqs(pop_name, self.pops_cfg, syn_base=self.base)
            pop = b2.NeuronGroup(N = gs**2, 
                                 name = pop_name, 
                                 model = eqs, 
                                 refractory = cell_cfg['ref'], #2*b2.ms, 
                                 threshold='v > {}*mV'.format(cell_cfg['thr']/b2.mV),
                                 reset='v={}*mV'.format(cell_cfg['rest']/b2.mV),
                                 method='euler'
                                 )
            pop.mu = noise_cfg['mu']
            pop.sigma = noise_cfg['sigma']
            pop.v = np.random.uniform(cell_cfg['rest']/b2.mV,
                                      cell_cfg['thr']/b2.mV,
                                      pop.N) *b2.mV

            
            # add coordinates
            pop.add_attribute('coord')
            y,x = np.indices((gs,gs))
            pop.coord = list(zip(x.ravel(),y.ravel()))
            
            self.pops[pop_name] = pop
            del x,y, cell_cfg, noise_cfg, gs, eqs
            
    
    def setup_landscape(self):
        """
        Generates the requested landscape: A dictionary keyed by ``phi`` and 
        ``r`` that indicated the dominant angle of anisotropy and the 
        radial displacement for every neuron in form of flat arrays.
        
        Landscapes are accessible via the `lscp` attribute of the ``Simulate``
        object, in form of a dictionary keyed by the population's name.
        """
        
        self.lscp = {}
        for conn_name in self.conn_cfg.keys():
            src, trg = conn_name
            
            lscp_cfg = self.conn_cfg[conn_name]['anisotropy']
            gs = self.pops_cfg[src]['gs'] # grid size
            
            rs, phis = make_landscape(gs=gs,
                                      ls_type = lscp_cfg['type'], 
                                      ls_params = lscp_cfg['params'])
            
            self.lscp[conn_name] = {'phi' : phis, 'r' :rs}
            
        
        del rs, phis, gs, lscp_cfg, src, trg
        
        
    def setup_syns(self, visualize=False):
        """
        Sets up the synapses of all pathways (keys) in ``conns_cfg``; a nested 
        dictionary whose detail is given in ``configs`` module. To understand 
        how the postsynpases are selected, particularly their radial profile 
        and the notion of anisotropy, please check `[1]`_.
        
        The connectivity matrix is saved as a sparse array (.npz) if it is not
        saved already.
       
        Synaoses are accessible via the `syns` attribute of the ``Simulate``
        object, in form of a list indexed by the sorted order of pathways.
        
        .. _[1]: https://doi.org/10.1371/journal.pcbi.1007432

        """
        
        self.syns = []
        for key in sorted(self.conn_cfg.keys()):
            src, trg = key
            
            eqs, on_pre, on_post = eq.get_syn_eqs(key, self.conn_cfg, self.base)
            ncons = self.conn_cfg[key]['ncons']
            spop = self.pops[src]
            tpop = self.pops[trg]
            
            syn = b2.Synapses(spop, tpop, 
                              model=eqs, 
                              on_pre=on_pre,
                              on_post=on_post,
                              delay=self.conn_cfg[key]['synapse']['params']['delay'],
                              method='exact'
                              )
            # load or save connectivity 
            w_name = 'w_'+self.name+'_'+key
            if self.load_connectivity:
                try:
                    print('\tLoading connectivity matrix: {}'.format(w_name))
                    w = sparse.load_npz(root +'results/data/'+w_name+'.npz')
                except:
                    print('\tWarning: Connecitivy file {} was not found.'.format(w_name))                    
                    print('\tWarning: Computing connectivity from scratch.')                    
                    self.load_connectivity=False
                    
            # computing anisotropic post-synapses
            for s_idx in range(len(spop)):
                if self.load_connectivity:
                    t_idxs = w.col[w.row==s_idx]
                else:
                    kws = dict(s_coord = spop.coord[s_idx],
                               ncons = ncons,
                               srow = int(np.sqrt(len(spop))),
                               scol = int(np.sqrt(len(spop))),
                               trow = int(np.sqrt(len(tpop))),
                               tcol = int(np.sqrt(len(tpop))),
                               shift = {'phi': self.lscp[key]['phi'][s_idx], 
                                        'r' : self.lscp[key]['r'][s_idx]},
                               profile = self.conn_cfg[key]['profile'],
                               self_link = self.conn_cfg[key]['self_link'],
                               recurrent = trg==src
                               )
                    s_coord, t_coords = get_post_syns(**kws) # projects s_coord
                    t_idxs = utils.coord2idx(t_coords, tpop)
                    
                syn.connect(i = s_idx, j = t_idxs)
            
            # set synaptic weight
            syn.J = self.conn_cfg[key]['synapse']['params']['J']
            
            # append to the class
            self.syns.append(syn)
            
            # save if not saved
            #set_trace()
            if not self.load_connectivity:
                row_idx = np.array(syn.i)
                col_idx = np.array(syn.j)
                data = np.ones_like(row_idx)
                w = sparse.coo_matrix((data, (row_idx, col_idx)))
                sparse.save_npz(root +'results/data/'+w_name+'.npz', w)
                #sim.w = w
                viz.plot_connectivity(root +'results/data/'+w_name+'.npz')
                
                del t_coords, s_coord, kws
        del src, trg, eqs, on_pre, on_post, ncons 
        del spop, tpop, syn, t_idxs, w
     
        
    def configure_monitors(self):
        """
        Defines a list of spike monitor called ``mon_<population_name>`` for 
        each population, and adds them to the ``Simulate`` object.
        
        The monitors are accessible via the `mons` attribute in form of a list 
        indexed by the sorted order of populations.
        
        :return: DESCRIPTION
        :rtype: TYPE

        """
        self.mons = []
        for pop_name in sorted(self.pops.keys()):
            # self.mons.append(b2.StateMonitor(self.pops[pop_name], 
            #                                  variables='v', 
            #                                  record=True))
            self.mons.append(b2.SpikeMonitor(self.pops[pop_name], 
                                             record=True, name='mon_'+pop_name))
        
    def warmup(self):
        """
        Warms up the neurons for 500 ms. In the first half neurons receive no
        input rather than a 500 pA (std) white noise. In the second half the
        background activity (mean and std) will be adjusted to their simulation 
        values.
        
        After the each warm-up, the states is saved on the disk for restoration
        for later simulations. 
        
        
        .. note: 
            Previous warm-up states will be overwritten by the freshest 
            execution.
        """
        
        # resetting the time
        self.net.t_ = 0
        
        # we don't need to record warm-up activities        
        for mon in self.mons:
            mon.active = False
        
        # Let's save mu and sigma for later and swich them off for now
        mus ={}
        stds = {}
        for pop in self.pops.values():
            mus[pop.name] = pop.mu/b2.pA
            stds[pop.name] = pop.sigma/b2.pA
            
            pop.mu = 0*b2.pA # turning background off
            pop.sigma = self.warmup_std # warm up noise
            
        print('Starting warm-up.')
        self.net.run(self.warmup_dur/2)
        for pop in self.pops.values():
            pop.mu = mus[pop.name]*b2.pA
            pop.sigma = stds[pop.name]*b2.pA
        self.net.run(self.warmup_dur/2)
        print('Finished warm up. Storing results.')
        
        self.net.store(name= self.name, filename=root +'results/data/warm-up_'+self.name)
        
        # switch on monitors        
        for mon in self.mons:
            mon.active = True
        
        del mus, stds            
    
        
    def start(self, duration=1000*b2.ms, batch_dur=200*b2.ms, 
              restore=True, profile=False, plot_rates=True):
        """
        Starts a long simulation by breaking it down to several batches. After
        each ``batch_dur``, the monitors will be saved on disk, and simulation
        monitors will be reset to combat memory consumption.
        
        :param duration: total duration of simualtion, excluding warm-up phase.
            defaults to 1000*b2.ms
        :type duration: Time quantitiy, optional
        :param batch_dur: duration of simulation batches, defaults to 200*b2.ms
        :type batch_dur: Time quantitiy, optional
        :param restore: whether or not restore the warmed-up stete from disk,
            defaults to True
        :type restore: bool, optional
        :param profile: whether or not profile the simulation (useful for 
            performance analysis), defaults to False
        :type profile: bool, optional
        :param plot_rates: whether or not plot the firing rate at the end of 
            each batch, defaults to True
        :type plot_rates: bool, optional
        """
        
        if restore:
            try:
                self.net.restore(name = self.name, 
                                 filename=root +'results/data/warm-up_'+self.name)
                print('Restored from warmed up state of {}'.format(self.name))
            
            except Exception as e: 
                print(str(e))
                print('Warning: Could not restore state from: warm-up_{}.'.format(self.name))
                self.warmup()
        
        print('Starting simulation.')
        nbatch = int(duration/batch_dur)
        if (duration-nbatch*batch_dur)/(b2.defaultclock.dt)>0:
            nbatch += 1
        
        for n in range(nbatch):
            self.reset_monitors()
            
            dur = min(batch_dur, duration-n*batch_dur)
            print('Starting batch number {}/{}'.format(n+1, nbatch))
            self.net.run(dur, profile=profile)
            
            filename = self.name
            if nbatch>1:
                filename += '_partial_{}'.format(n)
            viz.plot_firing_rates(sim=self, suffix='_'+str(n))
            
            self.save_monitors(filename = filename)
            
    
    # def post_process(self):
    #     print('Starting postprocessing ...')
    #     fig, axs = plt.subplots(2,1, figsize=(12,8), sharex=True, sharey=True)
    #     #set_trace()
    #     for id_, pop in enumerate(self.pops.values()):
    #         ax = axs[id_]        
    #         mon = self.mons[id_]
    #         for idx in range(pop.N):
    #             ax.plot(mon.t, mon.v[idx])
                
    #     plt.savefig(root +'results/LIF.png')
    
    def save_monitors(self, filename):
        for mon in self.mons:
            data = mon.get_states()
            with open(root +'results/data/'+filename+'_'+mon.name+'.dat', 'wb') as f:
                pickle.dump(data, f)
            
        del mon, data
    
    def make_txy(self):
        import glob
        
        for pop_name in sim.pops.keys():
            txy = []
            files_list = sorted(glob.glob(root +'results/data/mon_'+pop_name+'*.dat'))
            for file in files_list[-2:]:
                f = open(file, 'rb')
                f = f.read()
                data = pickle.loads(f)
                
                xy = utils.idx2coords(data['i'], sim.pops[pop_name])
                txy.append(np.hstack((data['t'].reshape(-1,1), xy)))
                del data,xy
                
            txy = np.concatenate(txy)
            print(txy.shape)
            np.savetxt(root +'results/data/txy_'+self.name+'_'+pop_name+'.csv', txy, 
                       fmt=('%f, %d, %d'))
            
    def reset_monitors(self):
        """
        Resets the monitors by removing them, redefining them, and adding them
        to the network again. This is necessary in Brian (look here:
        https://brian.discourse.group/t/how-to-reset-network-monitors/548)
        """
        self.net.remove(self.mons)
        self.configure_monitors()
        self.net.add(self.mons)
        
if __name__=='__main__':
    #pops_cfg, conn_cfg = configs.get_config('I_net') 
    sim = Simulate('I_net', scalar=1, load_connectivity=True)
    sim.setup_net()
    
    viz.plot_landscape(sim)
    viz.plot_in_out_deg(sim)
    #viz.plot_periodicity(sim)
    viz.plot_realized_landscape(sim)
    
    #time.sleep(5)
    
    #gs = int(np.sqrt(sim.pops['I'].N))
    #monI = b2.StateMonitor(sim.pops['I'], ['v', 'noise_I'], np.arange(gs)**2)
    #sim.net.add(monI)
    #sim.start()
    sim.warmup()
    sim.start(duration=2500*b2.ms, batch_dur=200*b2.ms, 
              restore=True, profile=False)
    #sim.plot_firing_rates()
    #for i in range(monI.v.shape[0]):
    #    plt.plot(monI.t, monI.v[i,:], alpha=0.1)
    viz.plot_animation(sim.name, gs = sim.pops_cfg['I']['gs']) 
    #sim.post_process()

from openmdao.core.group import Group
from openmdao.core.component import Component
from openmdao.components.indep_var_comp import IndepVarComp
from openmdao.components.exec_comp import ExecComp
from openmdao.drivers.scipy_optimizer import ScipyOptimizer

from pycycle.components.flow_start import FlowStart

from math import pi, sqrt

#from openmdao.main.api import Assembly
#from openmdao.lib.datatypes.api import Float, Int
#from openmdao.lib.drivers.api import BroydenSolver
#from openmdao.lib.casehandlers.api import CSVCaseRecorder


from cycle.compression_system import CompressionSystem
from cycle.splitter import SplitterW
#from tube_wall_temp import TubeWallTemp
from geometry.pod import Pod
from aero import Aero
#from tube_limit_flow import TubeLimitFlow
#from mission import Mission
#from run_cases import mva, mvr, mvb


class TubeFlow(Component):
    def __init__(self):
        super(TubeFlow, self).__init__()

        self.add_param('pod_MN', 0.5, desc='travel Mach of the pod')
        self.add_param('gamma', 1.41, desc='ratio of specific heats of air')
        self.add_param('tube_T', 292.6, desc='static temperature of tube', units='degK')
        self.add_param('tube_P', 99.0, desc='static pressure of tube', units='Pa')
        self.add_param('tube_area', 2.0, desc='cross sectional area of tube', units='m**2')
        self.add_param('R', 286.0, desc='specific gas constant for flow', units='m**2/s**2/degK')

        self.add_output('W', 0.0, desc='weight flow entering compression system', units='kg/s')
        self.add_output('Pt', 0.0, desc='total pressure of flow entering compression system', units='Pa')
        self.add_output('Tt', 0.0, desc='total temperature of flow entering compression system', units='degK')

    def solve_nonlinear(self, params, unknowns, resids):
        gam = params['gamma']
        MN = params['pod_MN']
        Ts = params['tube_T']
        Ps = params['tube_P']
        R = params['R']
        multiplier = (1.0 + (gam - 1.0) / 2.0 * MN ** 2)
        unknowns['Pt'] = Ps * multiplier ** (gam / (gam - 1.0))
        unknowns['Tt'] = Ts * multiplier
        unknowns['W'] = Ps / R / Ts * params['tube_area'] * MN * sqrt(gam * R * Ts)


class HyperloopSim(Group):
    def __init__(self):
        super(HyperloopSim, self).__init__()

        self.add('pod', Pod(), promotes=['cross_section', 'bypass_area', 'tube_P', 'tube_T', 'tube_r', 'tube_area', 'fill_area'])
        self.add('tube_flow', TubeFlow(), promotes=['pod_MN', 'tube_T', 'tube_P', 'tube_area'])
        self.add('start', FlowStart())
        self.add('W1_param', IndepVarComp('bypass_W', 0.2), promotes=['*'])
        self.add('split', SplitterW())
        self.add('compression_system', CompressionSystem())

        self.add('inlet_MN_param', IndepVarComp('inlet_MN', 0.5), promotes=['*'])
        self.add('bypass_MN_param', IndepVarComp('bypass_MN', 1.0), promotes=['*']) # choked by default

        self.add('bypass_area_con', ExecComp('c = ((area * 0.00065) - bypass_area) / bypass_area'), promotes=['bypass_area'])
        self.add('inlet_area_con', ExecComp('c = (((area * 0.00065) - inlet_area) ** 2) ** 0.5 / inlet_area'), promotes=['inlet_area'])

        self.connect('tube_flow.W', 'start.W')
        self.connect('tube_flow.Pt', 'start.P')
        self.connect('tube_flow.Tt', 'start.T')
        self.connect('pod_MN', 'start.MN_target')

        self.connect('bypass_W', 'split.W1')
        self.connect('bypass_MN', 'split.MN_out1_target')
        self.connect('inlet_MN', 'split.MN_out2_target')

        self.connect('split.Fl_O2:stat:MN', 'compression_system.diffuser.MN_out_target') # no diffuser
        
        self.connect('split.Fl_O1:stat:area', 'bypass_area_con.area')
        self.connect('split.Fl_O2:stat:area', 'inlet_area_con.area')

        CompressionSystem.connect_flow(self, 'start.Fl_O', 'split.Fl_I', connect_FAR=False)
        CompressionSystem.connect_flow(self, 'split.Fl_O2', 'compression_system.inlet.Fl_I', connect_stat=False, connect_FAR=False)

    @staticmethod
    def p_factory(tube_P=99.0, tube_T=292.6, pod_MN=0.2, inlet_area=0.785, cross_section=1.0, tube_r=0.9, fill_area=0.214, bypass_MN=0.8):
        from openmdao.core.problem import Problem
        from openmdao.units.units import convert_units as cu

        g = HyperloopSim()
        p = Problem(root=g, driver=ScipyOptimizer())
        p.driver.options['optimizer'] = 'COBYLA'
        
        p.setup(check=False)

        # tube flow
        p['tube_P'] = tube_P
        p['tube_T'] = tube_T
        p['pod_MN'] = pod_MN
        p['tube_r'] = tube_r
        p['fill_area'] = fill_area
        p['bypass_MN'] = bypass_MN

        # compression system
        p['inlet_area'] = inlet_area
        p['cross_section'] = cross_section
        p['compression_system.comp1.PR_design'] = 5.0
        p['compression_system.comp1.eff_design'] = 0.8
        p['compression_system.comp1_funnel.MN_out_target'] = 1.0 # keep internal MN greater than or equal to MN of bypass to avoid trailing vacuum
        p['compression_system.split.W1'] = 0.2
        p['compression_system.split.MN_out1_target'] = 1.0
        p['compression_system.split.MN_out2_target'] = 0.8
        p['compression_system.nozzle.dPqP'] = 0.0
        p['compression_system.nozzle.Ps_exhaust'] = cu(99.0, 'Pa', 'psi')
        p['compression_system.comp2.PR_design'] = 4.0
        p['compression_system.comp2.eff_design'] = 0.8
        p['compression_system.comp2_funnel.MN_out_target'] = 0.8

        p.driver.add_desvar('bypass_W', low=0.01, high=1000.0)
        p.driver.add_constraint('bypass_area_con.c', upper=0.01, lower=-0.01)
        p.driver.add_desvar('inlet_MN', low=0.01, high=1.0)
        p.driver.add_constraint('inlet_area_con.c', upper=0.01, lower=-0.01)
        p.driver.add_objective('inlet_area_con.c')

        return p


if __name__ == "__main__":
    p = HyperloopSim.p_factory(pod_MN=0.3, cross_section=1.5)
    p.run()

    print 'Total mass flow:                  ', p['split.split_calc.W_in'], 'kg/s'
    print 'Mass flow through bypass:         ', p['bypass_W'], 'kg/s'
    print 'Mass flow into compression system:', p['split.split_calc.W2'], 'kg/s'
    print 'Tube area:                        ', p['tube_area']
    print 'Cross section:                    ', p['cross_section']


#class HyperloopSim(Group):
#
#    def __init__(self):
#        super(HyperloopPod, self).__init__()
#
#        self.add('compression_system', CompressionSystem(), promotes=['pod_MN', 'Ps_tube'])
#        self.add('pod', Pod())
#        self.add('mission', Mission())
#        self.add('flow_limit', TubeLimitFlow())
#        #self.add('tube_wall_temp', TubeWallTemp)
#
#
#    #Design Variables
#    Mach_pod_max = Float(1.0, iotype="in", desc="travel Mach of the pod")
#    Mach_c1_in = Float(.6, iotype="in", desc="Mach number at entrance to the first compressor at design conditions")
#    Mach_bypass = Float(.95, iotype="in", desc="Mach in the air passing around the pod")
#    c1_PR_des = Float(12.47, iotype="in", desc="pressure ratio of first compressor at design conditions")
#    Ps_tube = Float(99, iotype="in", desc="static pressure in the tube", units="Pa", low=0)
#
#    #Parameters
#    solar_heating_factor = Float(.7, iotype="in",
#      desc="Fractional amount of solar radiation to consider in tube temperature calculations",
#      low=0, high=1)
#    tube_length = Float(563270, units = 'm', iotype='in', desc='Length of entire Hyperloop')
#    pwr_marg = Float(.3, iotype="in", desc="fractional extra energy requirement")
#    hub_to_tip = Float(.4, iotype="in", desc="hub to tip ratio for the compressor")
#    coef_drag = Float(2, iotype="in", desc="capsule drag coefficient")
#    n_rows = Int(14, iotype="in", desc="number of rows of seats in the pod")
#    length_row = Float(150, iotype="in", units="cm", desc="length of each row of seats")
#
#
#    def configure(self):
#
#        #Add Components
#        compress = self.add('compress', CompressionSystem())
#        mission = self.add('mission', Mission())
#        pod = self.add('pod', Pod())
#        flow_limit = self.add('flow_limit', TubeLimitFlow())
#        tube_wall_temp = self.add('tube_wall_temp', TubeWallTemp())
#
#        #Boundary Input Connections
#        #Hyperloop -> Compress
#        self.connect('Mach_pod_max', 'compress.Mach_pod_max')
#        self.connect('Ps_tube', 'compress.Ps_tube')
#        self.connect('Mach_c1_in','compress.Mach_c1_in') #Design Variable
#        self.connect('c1_PR_des', 'compress.c1_PR_des') #Design Variable
#        #Hyperloop -> Mission
#        self.connect('tube_length', 'mission.tube_length')
#        self.connect('pwr_marg','mission.pwr_marg')
#        #Hyperloop -> Flow Limit
#        self.connect('Mach_pod_max', 'flow_limit.Mach_pod')
#        self.connect('Ps_tube', 'flow_limit.Ps_tube')
#        self.connect('pod.radius_inlet_back_outer', 'flow_limit.radius_inlet')
#        self.connect('Mach_bypass','flow_limit.Mach_bypass')
#        #Hyperloop -> Pod
#        self.connect('Ps_tube', 'pod.Ps_tube')
#        self.connect('hub_to_tip','pod.hub_to_tip')
#        self.connect('coef_drag','pod.coef_drag')
#        self.connect('n_rows','pod.n_rows')
#        self.connect('length_row','pod.length_row')
#        #Hyperloop -> TubeWallTemp
#        self.connect('solar_heating_factor', 'tube_wall_temp.nn_incidence_factor')
#        self.connect('tube_length', 'tube_wall_temp.length_tube')
#
#        #Inter-component Connections
#        #Compress -> Mission
#        self.connect('compress.speed_max', 'mission.speed_max')
#        self.connect('compress.pwr_req', 'mission.pwr_req')
#        #Compress -> Pod
#        self.connect('compress.area_c1_in', 'pod.area_inlet_out')
#        self.connect('compress.area_inlet_in', 'pod.area_inlet_in')
#        self.connect('compress.rho_air', 'pod.rho_air')
#        self.connect('compress.F_net','pod.F_net')
#        self.connect('compress.speed_max', 'pod.speed_max')
#        #Compress -> TubeWallTemp
#        self.connect('compress.nozzle_Fl_O', 'tube_wall_temp.nozzle_air')
#        self.connect('compress.bearing_Fl_O', 'tube_wall_temp.bearing_air')
#        #Mission -> Pod
#        self.connect('mission.time','pod.time_mission')
#        self.connect('mission.energy', 'pod.energy')
#
#        #Add Solver
#        solver = self.add('solver',BroydenSolver())
#        solver.itmax = 50 #max iterations
#        solver.tol = .001
#        #Add Parameters and Constraints
#        solver.add_parameter('compress.W_in',low=-1e15,high=1e15)
#        solver.add_parameter('compress.c2_PR_des', low=-1e15, high=1e15)
#        solver.add_parameter(['compress.Ts_tube','flow_limit.Ts_tube','tube_wall_temp.temp_boundary'], low=-1e-15, high=1e15)
#        solver.add_parameter(['flow_limit.radius_tube', 'pod.radius_tube_inner'], low=-1e15, high=1e15)
#
#        solver.add_constraint('.01*(compress.W_in-flow_limit.W_excess) = 0')
#        solver.add_constraint('compress.Ps_bearing_residual=0')
#        solver.add_constraint('tube_wall_temp.ss_temp_residual=0')
#        solver.add_constraint('.01*(pod.area_compressor_bypass-compress.area_c1_out)=0')
#
#        driver = self.driver
#        driver.workflow.add('solver')
#        #driver.recorders = [CSVCaseRecorder(filename="hyperloop_data.csv")] #record only converged
#        #driver.printvars = ['Mach_bypass', 'Mach_pod_max', 'Mach_c1_in', 'c1_PR_des', 'pod.radius_inlet_back_outer',
#        #                    'pod.inlet.radius_back_inner', 'flow_limit.radius_tube', 'compress.W_in', 'compress.c2_PR_des',
#        #                    'pod.net_force', 'compress.F_net', 'compress.pwr_req', 'pod.energy', 'mission.time',
#        #                    'compress.speed_max', 'tube_wall_temp.temp_boundary']
#
#        #Declare Solver Workflow
#        solver.workflow.add(['compress','mission','pod','flow_limit','tube_wall_temp'])
#
#if __name__=="__main__":
#    from collections import OrderedDict
#    import numpy as np
#
#    hl = HyperloopPod()
#    #design variables
#    hl.Mach_bypass = .95
#    hl.Mach_pod_max = .90
#    hl.Mach_c1_in = .65
#    hl.c1_PR_des = 13
#
#    hl.configure()
#    #initial guesses
#    hl.compress.W_in = .35
#    hl.pod.configure()
#    hl.flow_limit.radius_tube = hl.pod.radius_tube_inner = 178
#    hl.compress.Ts_tube = hl.flow_limit.Ts_tube = hl.tube_wall_temp.tubeWallTemp = 322
#    hl.compress.c2_PR_des = 5
#
#    #mvr(hl) #mach vs radius
#
#    #mva(hl) #mach vs area ratio
#
#    #mvb(hl) #mach vs battery/comp/missionTime
#
#    design_data = OrderedDict([
#        ('Mach bypass', hl.Mach_bypass),
#        ('Max Travel Mach', hl.Mach_pod_max),
#        ('Fan Face Mach', hl.Mach_c1_in),
#        ('C1 PR', hl.c1_PR_des)
#    ])
#
#    output_data = OrderedDict([
#        ('Radius Inlet Outer',  hl.pod.radius_inlet_back_outer),
#        ('Radius Inlet Inner',  hl.pod.inlet.radius_back_inner),
#        ('Tube Inner Radius', hl.flow_limit.radius_tube),
#        ('Pod W', hl.compress.W_in),
#        ('Compressor C2 PR', hl.compress.c2_PR_des),
#        ('Pod Net Force', hl.pod.net_force),
#        ('Pod Thrust', hl.compress.F_net),
#        ('Pod Power', hl.compress.pwr_req),
#        ('Total Energy', hl.pod.energy),
#        ('Travel time', hl.mission.time),
#        ('Max Speed', hl.compress.speed_max),
#        ('Equilibirum Tube Temp', hl.tube_wall_temp.temp_boundary)
#    ])
#
#    def pretty_print(data):
#        for label,value in data.iteritems():
#            print '%s: %.2f'%(label,value)
#
#
#    print "======================"
#    print "Design"
#    print "======================"
#    pretty_print(design_data)
#
#    print "======================"
#    print "Performance"
#    print "======================"
#    pretty_print(output_data)

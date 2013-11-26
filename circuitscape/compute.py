__version__ = '4.0.0-beta'
__author__ = 'Brad McRae, Viral B. Shah, and Tanmay Mohapatra'
__email__ = 'mcrae@circuitscape.org'


import time, logging, os, pickle
import numpy as np
from scipy import sparse

from compute_base import ComputeBase, FocalPoints, HabitatGraph, Output
from profiler import print_rusage, gc_after, LowMemRetry
from io import CSIO

class Compute(ComputeBase):
    def __init__(self, configFile, ext_log_handler):
        super(Compute, self).__init__(configFile, ext_log_handler)

    @print_rusage
    def compute(self):
        """Main function for Circuitscape."""  
        # Code below provides a back door to network mode, because not incorporated into GUI yet
        if self.options.polygon_file == 'NETWORK': 
            self.options.data_type='network' #can also be set in .ini file
        if self.options.data_type=='network': 
            self.options.graph_file = self.options.habitat_file
            self.options.focal_node_file = self.options.point_file

        self.state.start_time = time.time()

        #Test write privileges by writing config file to output directory
        self.options.write(self.options.output_file, True)
        
        if self.options.data_type=='network':
            result, solver_failed = self.compute_network() # Call module for solving arbitrary graphs (not raster grids)
            self.log_complete_job()
            return result, solver_failed #Fixme: add in solver failed check

        return self.compute_raster()


    @print_rusage
    def compute_network(self): 
        """Solves arbitrary graphs instead of raster grids."""
        (g_graph, node_names) = self.read_graph(self.options.graph_file)
        focal_nodes = self.read_focal_nodes(self.options.focal_node_file)
        
        if self.options.use_included_pairs==True:
            self.state.included_pairs = CSIO.read_included_pairs(self.options.included_pairs_file)
        
        fp = FocalPoints(focal_nodes, self.state.included_pairs, True)
        g_habitat = HabitatGraph(g_graph=g_graph, node_names=node_names)
        out = Output(self.options, self.state, False, (g_habitat.num_nodes, g_habitat.num_nodes))
        if self.options.write_cur_maps:
            out.alloc_c_map('')
        
        (resistances, solver_failed) = self.single_ground_all_pair_resistances(g_habitat, fp, out, True)
        _resistances, resistances_3col = self.write_resistances(fp.point_ids, resistances)
        if self.options.write_cur_maps:
            full_branch_currents, full_node_currents, _bca, _np = out.get_c_map('')
            full_branch_currents = Output._convert_graph_to_3_col(full_branch_currents, node_names)
            full_node_currents = Output._append_names_to_node_currents(full_node_currents, node_names)

            ind = np.lexsort((full_branch_currents[:, 1], full_branch_currents[:, 0]))
            full_branch_currents = full_branch_currents[ind]

            ind = np.lexsort((full_node_currents[:, 1], full_node_currents[:, 0]))
            full_node_currents = full_node_currents[ind]

            CSIO.write_currents(self.options.output_file, full_branch_currents, full_node_currents, '')
            
        return resistances_3col, solver_failed

    @gc_after
    def read_graph(self, filename):
        """Reads arbitrary graph from disk. Returns sparse adjacency matrix and node names ."""
        graph_list = CSIO.load_graph(filename)

        try:
            zeros_in_resistance_graph = False           
            nodes = ComputeBase.deletecol(graph_list,2) 
            node_names = np.unique(np.asarray(nodes, dtype='int32'))
            nodes[np.where(nodes>= 0)] = ComputeBase.relabel(nodes[np.where(nodes>= 0)], 0)
            node1 = nodes[:,0]
            node2 = nodes[:,1]
            data = graph_list[:,2]
            
            ######################## Reclassification code
            if self.options.use_reclass_table == True:
                try:
                    reclass_table = CSIO.read_point_strengths(self.options.reclass_file)    
                except:
                    raise RuntimeError('Error reading reclass table.  Please check file format.')
                for i in range (0,reclass_table.shape[0]):
                    data = np.where(data==reclass_table[i,0], reclass_table[i,1],data)
                Compute.logger.debug('Reclassified habitat graph using %s'%(self.options.reclass_file,))
            ########################
            
            if self.options.habitat_map_is_resistances == True:
                zeros_in_resistance_graph = (np.where(data==0, 1, 0)).sum() > 0
                conductances = 1/data
            else:
                conductances = data
                
            numnodes = node_names.shape[0]
            G = sparse.csr_matrix((conductances, (node1, node2)), shape = (numnodes, numnodes))

            Gdense=G.todense()
            g_graph = np.maximum(Gdense, Gdense.T) # To handle single or double entries for elements BHM 06/28/11
            g_graph = sparse.csr_matrix(g_graph)
        except:
            raise RuntimeError('Error processing graph/network file.  Please check file format')
        
        if zeros_in_resistance_graph == True:
            raise RuntimeError('Error: zero resistance values are not currently allowed in habitat network/graph input file.')
        
        return g_graph, node_names


    def read_focal_nodes(self, filename):
        """Loads list of focal nodes for arbitrary graph."""  
        focal_nodes = CSIO.load_graph(filename)
        try:    
            if filename == self.options.graph_file:#If graph was used as focal node file, then just want first two columns for focal_nodes.
                focal_nodes = ComputeBase.deletecol(focal_nodes, 2)
            focal_nodes = np.unique(np.asarray(focal_nodes))
        except:
            raise RuntimeError('Error processing focal node file.  Please check file format')
        return focal_nodes


    @print_rusage
    def compute_raster(self):
        self.load_maps()
        if self.options.screenprint_log == True:        
            num_nodes = (np.where(self.state.g_map > 0, 1, 0)).sum()         
            Compute.logger.debug('Resistance/conductance map has %d nodes' % (num_nodes,))

        if self.options.scenario == 'pairwise':
            resistances, solver_failed = self.pairwise_module(self.state.g_map, self.state.poly_map, self.state.points_rc)
            self.log_complete_job()
            return resistances,solver_failed     

        elif self.options.scenario == 'advanced':
            self.options.write_max_cur_maps = False
            Compute.logger.info('Calling solver module.')
            g_habitat = HabitatGraph(g_map=self.state.g_map, poly_map=self.state.poly_map, connect_using_avg_resistances=self.options.connect_using_avg_resistances, connect_four_neighbors_only=self.options.connect_four_neighbors_only)
            out = Output(self.options, self.state, False)
            voltages, _current_map, solver_failed = self.advanced_module(g_habitat, out, self.state.source_map, self.state.ground_map)
            self.log_complete_job()
            if solver_failed == True:
                Compute.logger.error('Solver failed')
            return voltages, solver_failed
        
        else:
            resistance_vector, solver_failed = self.one_to_all_module(self.state.g_map, self.state.poly_map, self.state.points_rc)
            self.log_complete_job()
            return resistance_vector, solver_failed 
            
    
  
    def get_overlap_polymap(self, point, point_map, poly_map, new_poly_num): 
        """Creates a map of polygons (aka short-circuit or zero resistance regions) overlapping a focal node."""
        point_poly = np.where(point_map == point, 1, 0) 
        poly_point_overlap = np.multiply(point_poly, poly_map)
        overlap_vals = np.unique(np.asarray(poly_point_overlap))
        rows = np.where(overlap_vals > 0)
        overlap_vals = overlap_vals[rows] #LIST OF EXISTING POLYGONS THAT OVERLAP WITH POINT
        for a in range(0, overlap_vals.size):
            poly_map = np.where(poly_map==overlap_vals[a], new_poly_num, poly_map)
        poly_map = np.where(point_map == point, new_poly_num, poly_map)
        return poly_map


    def append_names_to_resistances(self, point_ids, resistances):        
        """Adds names of focal nodes to resistance matrices."""  
        focal_labels = np.insert(point_ids, [0], 0, axis = 0)
        resistances = np.insert(resistances, [0], 0, axis = 0)
        resistances = np.insert(resistances, [0], 0, axis = 1)
        resistances[0,:] = focal_labels
        resistances[:,0] = focal_labels
        return resistances
        
    
    @print_rusage
    def one_to_all_module(self, g_map, poly_map, points_rc):
        """Overhead module for one-to-all AND all-to-one modes with raster data."""  
        last_write_time = time.time()

        out = Output(self.options, self.state, False)
        if self.options.write_cur_maps:
            out.alloc_c_map('')
            if self.options.write_max_cur_maps:
                out.alloc_c_map('max')
        
        fp = FocalPoints(points_rc, self.state.included_pairs, False)
        fp = FocalPoints(fp.get_unique_coordinates(), self.state.included_pairs, False)
        point_ids = fp.point_ids
        points_rc_unique = fp.points_rc
        included_pairs = self.state.included_pairs
            
        resistance_vector = np.zeros((point_ids.size,2), float)
        solver_failed_somewhere = False
        
        if self.options.use_included_pairs==False: #Will do this each time later if using included pairs
            point_map = np.zeros((self.state.nrows, self.state.ncols), int)
            point_map[points_rc[:,1], points_rc[:,2]] = points_rc[:,0]
       
            #combine point map and poly map
            poly_map_temp = self.get_poly_map_temp(poly_map, point_map, point_ids)
            unique_point_map = np.zeros((self.state.nrows, self.state.ncols), int)
            unique_point_map[points_rc_unique[:,1], points_rc_unique[:,2]] = points_rc_unique[:,0]

            (strength_map, strengths_rc) = self.get_strength_map(points_rc_unique, self.state.point_strengths)            

            g_habitat = HabitatGraph(g_map=g_map, poly_map=poly_map_temp, connect_using_avg_resistances=self.options.connect_using_avg_resistances, connect_four_neighbors_only=self.options.connect_four_neighbors_only)
            Compute.logger.debug('Graph has ' + str(g_habitat.num_nodes) + ' nodes and '+ str(g_habitat.num_components) + ' components.')
            component_with_points = g_habitat.unique_component_with_points(unique_point_map)
        else:
            g_habitat = HabitatGraph(g_map=g_map, poly_map=poly_map, connect_using_avg_resistances=self.options.connect_using_avg_resistances, connect_four_neighbors_only=self.options.connect_four_neighbors_only)
            Compute.logger.debug('Graph has ' + str(g_habitat.num_nodes) + ' nodes and '+ str(g_habitat.num_components) + ' components.')
            component_with_points = None

        for pt_idx in range(0, point_ids.size): # These are the 'src' nodes, pt_idx.e. the 'one' in all-to-one and one-to-all

            Compute.logger.info('solving focal node ' + str(pt_idx+1) + ' of ' + str(point_ids.size))

            if self.options.use_included_pairs==True: # Done above otherwise    
                #######################   
                points_rc_unique_temp = np.copy(points_rc_unique)
                point_map = np.zeros((self.state.nrows, self.state.ncols), int)
                point_map[points_rc[:,1], points_rc[:,2]] = points_rc[:,0]       

                #loop thru exclude[point,:], delete included pairs of focal point from point_map and points_rc_unique_temp
                for pair in range(0, point_ids.size):  
                    if (included_pairs[pt_idx+1, pair+1] == 0) and (pt_idx !=  pair):
                        pt_id = point_ids[pair]
                        point_map = np.where(point_map==pt_id, 0, point_map)
                        points_rc_unique_temp[pair, 0] = 0 #point will not be burned in to unique_point_map

                poly_map_temp = self.get_poly_map_temp2(poly_map, point_map, points_rc_unique_temp, included_pairs, pt_idx)
                g_habitat = HabitatGraph(g_map=g_map, poly_map=poly_map_temp, connect_using_avg_resistances=self.options.connect_using_avg_resistances, connect_four_neighbors_only=self.options.connect_four_neighbors_only)

                unique_point_map = np.zeros((self.state.nrows, self.state.ncols),int)
                unique_point_map[points_rc_unique_temp[:,1], points_rc_unique_temp[:,2]] = points_rc_unique_temp[:,0]
                
                (strength_map, strengths_rc) = self.get_strength_map(points_rc_unique_temp, self.state.point_strengths)
                ###########################################
                
            src = point_ids[pt_idx]
            if unique_point_map.sum() == src: # src is the only point
                resistance_vector[pt_idx,0] = src
                resistance_vector[pt_idx,1] = -1            
            else: # there are points to connect with src point
                if self.options.scenario == 'one-to-all':
                    strength = strengths_rc[pt_idx,0] if self.options.use_variable_source_strengths else 1
                    source_map = np.where(unique_point_map == src, strength, 0)
                    ground_map =  np.where(unique_point_map == src, 0, unique_point_map)
                    ground_map =  np.where(ground_map, np.Inf, 0) 
                    self.options.remove_src_or_gnd = 'rmvgnd'
                else: # all-to-one
                    if self.options.use_variable_source_strengths==True:
                        source_map = np.where(unique_point_map == src, 0, strength_map)
                    else:
                        source_map = np.where(unique_point_map, 1, 0)
                        source_map = np.where(unique_point_map == src, 0, source_map)
                    ground_map =  np.where(unique_point_map == src, np.Inf, 0)                    
                    self.options.remove_src_or_gnd = 'rmvsrc'
                #FIXME: right now one-to-all *might* fail if there is just one node that is not grounded (I haven't encountered this problem lately BHM Nov 2009).
                
                resistance, current_map, solver_failed = self.advanced_module(g_habitat, out, source_map, ground_map, src, component_with_points)
                if not solver_failed:
                    if self.options.write_cur_maps:
                        out.accumulate_c_map_with_values('', current_map)
                        if self.options.write_max_cur_maps:    
                            out.store_max_c_map_values('max', current_map)
                else:
                    Compute.logger.warning('Solver failed for at least one focal node.  \nFocal nodes with failed solves will be marked with value of -777 \nin output resistance list.\n')
    
                resistance_vector[pt_idx,0] = src
                resistance_vector[pt_idx,1] = resistance
                    
                if solver_failed==True:
                    solver_failed_somewhere = True

            (hours,mins,_secs) = ComputeBase.elapsed_time(last_write_time)
            if mins > 2 or hours > 0: 
                last_write_time = time.time()
                CSIO.write_resistances_one_to_all(self.options.output_file, resistance_vector, '_incomplete', self.options.scenario)
      
        if not solver_failed_somewhere:
            if self.options.write_cur_maps:
                out.write_c_map('', True)
                if self.options.write_max_cur_maps:
                    out.write_c_map('max', True)

        CSIO.write_resistances_one_to_all(self.options.output_file, resistance_vector, '', self.options.scenario)
       
        return resistance_vector, solver_failed_somewhere 

    def get_poly_map_temp(self, poly_map, point_map, point_ids):
        """Returns polygon map for each solve given source and destination nodes.  
        Used in all-to-one and one-to-all modes only.
        """  
        if poly_map == []:
            poly_map_temp = point_map
        else:
            poly_map_temp = poly_map
            new_poly_num = np.max(poly_map)
            for pt_idx in range(0, point_ids.shape[0]):
                new_poly_num = new_poly_num+1
                poly_map_temp = self.get_overlap_polymap(point_ids[pt_idx], point_map, poly_map_temp, new_poly_num) 
                
        return poly_map_temp

        
    def get_poly_map_temp2(self, poly_map, point_map, points_rc, included_pairs, pt1_idx):
        """Returns polygon map for each solve given source and destination nodes.  
        Used in all-to-one and one-to-all modes when included/excluded pairs are used.
        """  
        if poly_map == []:
            poly_map_temp = point_map
        else:
            poly_map_temp = poly_map
            new_poly_num = np.max(poly_map)
            #burn in src pt_idx to polygon map
            poly_map_temp = self.get_overlap_polymap(points_rc[pt1_idx,0], point_map, poly_map_temp, new_poly_num)                     
            for pt_idx in range(0, points_rc.shape[0]): #burn in dst points to polygon map
                if included_pairs[pt1_idx+1, pt_idx+1] == 1:  
                    new_poly_num = new_poly_num+1
                    poly_map_temp = self.get_overlap_polymap(points_rc[pt_idx,0], point_map, poly_map_temp, new_poly_num) 
        return poly_map_temp


    @print_rusage
    def pairwise_module(self, g_map, poly_map, points_rc):
        """Overhead module for pairwise mode with raster data."""  
        out = Output(self.options, self.state, False)
        if self.options.write_cur_maps:
            out.alloc_c_map('')
            if self.options.write_max_cur_maps:
                out.alloc_c_map('max')

        # If there are no focal regions, pass all points to single_ground_all_pair_resistances,
        # otherwise, pass one point at a time.
        if self.options.point_file_contains_polygons == False:
            if points_rc.shape[0] != (np.unique(np.asarray(points_rc[:,0]))).shape[0]:
                raise RuntimeError('At least one focal node contains multiple cells.  If this is what you really want, then choose focal REGIONS in the pull-down menu') 

            fp = FocalPoints(points_rc, self.state.included_pairs, False)
            g_habitat = HabitatGraph(g_map=g_map, poly_map=poly_map, connect_using_avg_resistances=self.options.connect_using_avg_resistances, connect_four_neighbors_only=self.options.connect_four_neighbors_only)
            
            while LowMemRetry.retry():
                with LowMemRetry():
                    (resistances, solver_failed) = self.single_ground_all_pair_resistances(g_habitat, fp, out, True)
                    
            if solver_failed == True:
                Compute.logger.warning('Solver failed for at least one focal node pair. ' 
                '\nThis can happen when input resistances differ by more than' 
                '\n~6 orders of magnitude. Pairs with failed solves will be '
                '\nmarked with value of -777 in output resistance matrix.\n')

            point_ids = points_rc[:,0]

        else:
            point_map = np.zeros((self.state.nrows, self.state.ncols), int)
            point_map[points_rc[:,1], points_rc[:,2]] = points_rc[:,0]
            
            fp = FocalPoints(points_rc, self.state.included_pairs, False)
            fp = FocalPoints(fp.get_unique_coordinates(), self.state.included_pairs, False)
            point_ids = fp.point_ids
            
            numpoints = point_ids.size
            resistances = -1 * np.ones((numpoints, numpoints), dtype='float64')
            
            num_points_solved = 0
            num_points_to_solve = numpoints*(numpoints-1)/2
            
            for (pt1_idx, pt2_idx) in fp.point_pair_idxs():
                if pt2_idx == -1:
                    continue    # we don't need to do anything special for row end condition
                
                if poly_map == []:
                    poly_map_temp = np.zeros((self.state.nrows, self.state.ncols), int)
                    new_poly_num = 1
                else:
                    poly_map_temp = poly_map
                    new_poly_num = np.max(poly_map) + 1
                
                poly_map_temp = self.get_overlap_polymap(point_ids[pt1_idx], point_map, poly_map_temp, new_poly_num) 
                poly_map_temp = self.get_overlap_polymap(point_ids[pt2_idx], point_map, poly_map_temp, new_poly_num+1) 
            
                # create a subset of points_rc by getting first instance of each point in points_rc
                fp_subset = fp.get_subset([pt1_idx, pt2_idx])
                g_habitat = HabitatGraph(g_map=g_map, poly_map=poly_map_temp, connect_using_avg_resistances=self.options.connect_using_avg_resistances, connect_four_neighbors_only=self.options.connect_four_neighbors_only)
            
                num_points_solved += 1
                Compute.logger.info('solving focal pair ' + str(num_points_solved) + ' of '+ str(num_points_to_solve))
            
                (pairwise_resistance, solver_failed) = self.single_ground_all_pair_resistances(g_habitat, fp_subset, out, False)

                del poly_map_temp
                if solver_failed == True:
                    Compute.logger.warning('Solver failed for at least one focal node pair.  \nPairs with failed solves will be marked with value of -777 \nin output resistance matrix.\n')

                resistances[pt2_idx, pt1_idx] = resistances[pt1_idx, pt2_idx] = pairwise_resistance[0,1]

        # Set diagonal to zero
        for i in range(0,resistances.shape[0]): 
            resistances[i, i] = 0

        # Add row and column headers and write resistances to disk
        resistances, _resistances_3col = self.write_resistances(point_ids, resistances)
        if self.options.write_cur_maps:
            out.write_c_map('')
            if self.options.write_max_cur_maps:
                out.write_c_map('max')

        return resistances,solver_failed


    @print_rusage
    def single_ground_all_pair_resistances(self, g_habitat, fp, cs, report_status):
        """Handles pairwise resistance/current/voltage calculations.  
        
        Called once when focal points are used, called multiple times when focal regions are used.
        """
        options = self.options
        last_write_time = time.time()
        numpoints = fp.num_points()
        parallelize = options.parallelize

        # TODO: revisit to see if restriction can be removed 
        if options.low_memory_mode==True or options.point_file_contains_polygons==True:
            parallelize = False
        
        if (options.point_file_contains_polygons == True) or  (options.write_cur_maps == True) or (options.write_volt_maps == True) or (options.use_included_pairs==True):
            use_resistance_calc_shortcut = False
        else:     
            use_resistance_calc_shortcut = True # We use this when there are no focal regions.  It saves time when we are also not creating maps
            shortcut_resistances = -1 * np.ones((numpoints, numpoints), dtype='float64') 
           
        solver_failed_somewhere = [False]
        
        Compute.logger.debug('Graph has ' + str(g_habitat.num_nodes) + ' nodes, ' + str(numpoints) + ' focal points and '+ str(g_habitat.num_components)+ ' components.')
        resistances = -1 * np.ones((numpoints, numpoints), dtype = 'float64')         #Inf creates trouble in python 2.5 on Windows. Use -1 instead.
        
        if use_resistance_calc_shortcut==True:
            num_points_to_solve = numpoints
        else:
            num_points_to_solve = numpoints*(numpoints-1)/2
        
        num_points_solved = 0
        for c in range(1, int(g_habitat.num_components+1)):
            if not fp.exists_points_in_component(c, g_habitat):
                continue
                        
            G_pruned, local_node_map = g_habitat.prune_nodes_for_component(c)
            G = ComputeBase.laplacian(G_pruned)
            del G_pruned 
            
            if use_resistance_calc_shortcut:
                voltmatrix = np.zeros((numpoints,numpoints), dtype='float64')     #For resistance calc shortcut

            G_dst_dst = local_dst = None
            for (pt1_idx, pt2_idx) in fp.point_pair_idxs_in_component(c, g_habitat):
                if pt2_idx == -1:
                    if parallelize:
                        self.state.worker_pool_wait()
                        
                    self.state.del_amg_hierarchy()
                    
                    if (local_dst != None) and (G_dst_dst != None):
                        G[local_dst, local_dst] = G_dst_dst
                        local_dst = G_dst_dst = None
                    
                    if (use_resistance_calc_shortcut==True):
                        Compute.get_shortcut_resistances(pt1_idx, voltmatrix, numpoints, resistances, shortcut_resistances)
                        break #No need to continue, we've got what we need to calculate resistances
                    else:
                        continue

                if parallelize:
                    self.state.worker_pool_create(options.max_parallel, True)

                if report_status==True:
                    num_points_solved += 1
                    if use_resistance_calc_shortcut==True:
                        Compute.logger.info('solving focal node ' + str(num_points_solved) + ' of '+ str(num_points_to_solve))
                    else:
                        Compute.logger.info('solving focal pair ' + str(num_points_solved) + ' of '+ str(num_points_to_solve))
            
                local_src = fp.get_graph_node_idx(pt2_idx, local_node_map)
                if None == local_dst:
                    local_dst = fp.get_graph_node_idx(pt1_idx, local_node_map)
                    G_dst_dst = G[local_dst, local_dst]
                    G[local_dst, local_dst] = 0

                if self.state.amg_hierarchy == None:
                    self.state.create_amg_hierarchy(G, self.options.solver)

                if use_resistance_calc_shortcut:
                    post_solve = self._post_single_ground_solve(G, fp, cs, resistances, numpoints, pt1_idx, pt2_idx, local_src, local_dst, local_node_map, solver_failed_somewhere, use_resistance_calc_shortcut, voltmatrix)
                else:
                    post_solve = self._post_single_ground_solve(G, fp, cs, resistances, numpoints, pt1_idx, pt2_idx, local_src, local_dst, local_node_map, solver_failed_somewhere)
                
                if parallelize:
                    self.state.worker_pool.apply_async(Compute.parallel_single_ground_solver, args=(G, local_src, local_dst, options.solver, self.state.amg_hierarchy), callback=post_solve)
                    #post_solve(self.state.worker_pool.apply(Compute.parallel_single_ground_solver, args=(G, local_src, local_dst, options.solver, self.state.amg_hierarchy)))
                else:
                    try:
                        voltages = Compute.single_ground_solver(G, local_src, local_dst, options.solver, self.state.amg_hierarchy)
                    except:
                        voltages = None
                    post_solve(voltages)

                if options.low_memory_mode==True or options.point_file_contains_polygons==True:
                    self.state.del_amg_hierarchy()
    
            (hours,mins,_secs) = ComputeBase.elapsed_time(last_write_time)
            if mins > 2 or hours > 0: 
                last_write_time = time.time()
                CSIO.save_incomplete_resistances(options.output_file, resistances)# Save incomplete resistances    

        self.state.del_amg_hierarchy()

        # Finally, resistance to self is 0.
        if use_resistance_calc_shortcut==True: 
            resistances = shortcut_resistances
        for i in range(0, numpoints):
            resistances[i, i] = 0

        return resistances, solver_failed_somewhere[0]


    def _post_single_ground_solve(self, G, fp, cs, resistances, numpoints, pt1_idx, pt2_idx, local_src, local_dst, local_node_map, solver_failed_somewhere, use_resistance_calc_shortcut=False, voltmatrix=None):
        def _post_callback(voltages):
            options = self.options
            
            if voltages == None:
                solver_failed_somewhere[0] = True
                resistances[pt2_idx, pt1_idx] = resistances[pt1_idx, pt2_idx] = -777
                return
            
            resistances[pt2_idx, pt1_idx] = resistances[pt1_idx, pt2_idx] = voltages[local_src] - voltages[local_dst]
            
            # Write maps to files
            frompoint = int(fp.point_id(pt1_idx))
            topoint = int(fp.point_id(pt2_idx))
                    
            if use_resistance_calc_shortcut:
                self.get_voltmatrix(pt1_idx, pt2_idx, numpoints, local_node_map, voltages, fp, resistances, voltmatrix)
            else:
                cv_map_name = str(frompoint)+'_'+str(topoint)
                cs.write_v_map(cv_map_name, False, voltages, local_node_map)
                if options.write_cur_maps:
                    finitegrounds = [-9999] #create dummy value for pairwise case
                    if options.write_cum_cur_map_only:
                        cs.store_c_map(cv_map_name, voltages, G, local_node_map, finitegrounds, local_src, local_dst)
                    else:
                        cs.write_c_map(cv_map_name, False, voltages, G, local_node_map, finitegrounds, local_src, local_dst)
                    cs.accumulate_c_map_from('', cv_map_name)
                    if options.write_max_cur_maps:
                        cs.store_max_c_map('max', cv_map_name)
                    cs.rm_c_map(cv_map_name)
    
        return _post_callback

    @staticmethod
    def parallel_single_ground_solver(G, src, dst, solver_type, ml):
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if (0 == child_pid):
            pid = str(os.getpid())
            logging.disable(logging.CRITICAL) # disable logging in child to avoid python bug : http://bugs.python.org/issue6721
            try:
                os.close(read_fd)
                write_file = os.fdopen(write_fd, 'w')
                result = Compute.single_ground_solver(G, src, dst, solver_type, ml)
            except Exception as e:
                result = e
            write_file.write(pickle.dumps(result))
            write_file.close()
            os._exit(os.EX_OK)
        elif (child_pid > 0):
            pid = str(child_pid)
            try:
                #Compute.logger.debug("parallel: waiting for " + pid)
                os.close(write_fd)
                read_file = os.fdopen(read_fd)
                result = read_file.read()
                voltages = pickle.loads(result)
                #Compute.logger.debug("parallel: got results from " + pid)
                if isinstance(voltages, Exception):
                    Compute.logger.exception("parallel: got error from " + pid + ": " + str(voltages))
                    voltages = None
            except:
                Compute.logger.exception("parallel: exception waiting for results from " + pid)
                voltages = None
            finally:
                read_file.close()
                #Compute.logger.debug("parallel: waiting for termination of " + pid)
                os.waitpid(child_pid, 0)
                #Compute.logger.debug("parallel: terminated " + pid)
        else:
            Compute.logger.error("parallel: unable to create new processes")
            voltages = None
        return voltages
        
    @staticmethod
    @print_rusage
    def single_ground_solver(G, src, dst, solver_type, ml):
        """Solver used for pairwise mode."""  
        n = G.shape[0]
        rhs = np.zeros(n, dtype = 'float64')
        if src==dst:
            voltages = np.zeros(n, dtype = 'float64')
        else:
            rhs[dst] = -1
            rhs[src] = 1
            voltages = ComputeBase.solve_linear_system (G, rhs, solver_type, ml)

        return voltages

    @print_rusage
    def advanced_module(self, g_habitat, cs, source_map, ground_map, source_id=None, component_with_points=None):
        solver_called = False
        solver_failed = False
         
        if self.options.scenario=='advanced':
            Compute.logger.debug('Graph has ' + str(g_habitat.num_nodes) + ' nodes and '+ str(g_habitat.num_components)+ ' components.')

        if component_with_points != None:
            G = g_habitat.get_graph()
            G = ComputeBase.laplacian(G)
            node_map = g_habitat.node_map
        
        vc_map_id = '' if source_id==None else str(source_id)
        cs.alloc_v_map(vc_map_id)
        
        if self.options.write_cur_maps:
            cs.alloc_c_map(vc_map_id)
            
        for comp in range(1, g_habitat.num_components+1):
            if (component_with_points != None) and (comp != component_with_points):
                continue

            c_map = np.where(g_habitat.component_map == comp, 1, 0)
            local_source_map = np.multiply(c_map, source_map)
            local_ground_map = np.where(c_map, ground_map, 0) 
            del c_map
            
            source_in_component = (np.where(local_source_map, 1, 0)).sum() > 0
            ground_in_component = (np.where(local_ground_map, 1, 0)).sum() > 0
            
            if (source_in_component) & (ground_in_component):
                (rows, cols) = np.where(local_source_map)
                values = local_source_map[rows,cols]
                local_sources_rc = np.c_[values,rows,cols]
                (rows, cols) = np.where(local_ground_map)
                values = local_ground_map[rows,cols]
                local_grounds_rc = np.c_[values,rows,cols]
                del rows, cols, values, local_source_map, local_ground_map 

                if component_with_points == None:
                    (G, node_map) = g_habitat.prune_nodes_for_component(comp)
                    G = ComputeBase.laplacian(G)

                numnodes = node_map.max()
                sources = np.zeros(numnodes)
                grounds = np.zeros(numnodes)
                num_local_sources = local_sources_rc.shape[0]
                num_local_grounds = local_grounds_rc.shape[0]

                for source in range(0, num_local_sources):
                    src = self.grid_to_graph (local_sources_rc[source,1], local_sources_rc[source,2], node_map)
                    # Possible to have more than one source at a node when there are polygons
                    sources[src] = sources[src] + local_sources_rc[source,0] 

                for ground in range(0, num_local_grounds):
                    gnd = self.grid_to_graph (local_grounds_rc[ground,1], local_grounds_rc[ground,2], node_map)
                    # Possible to have more than one ground at a node when there are polygons
                    grounds[gnd] = grounds[gnd] + local_grounds_rc[ground,0] 

                (sources, grounds, finitegrounds) = self.resolve_conflicts(sources, grounds)

                solver_called = True
                try:
                    voltages = self.multiple_solver(G, sources, grounds, finitegrounds) 
                    del sources, grounds
                except MemoryError:
                    raise MemoryError
                except:
                    voltages = -777
                    solver_failed = True

                if solver_failed==False:
                    ##Voltage and current mapping are cumulative, since there may be independent components.
                    if self.options.write_volt_maps or (self.options.scenario=='one-to-all'):
                        cs.accumulate_v_map(vc_map_id, voltages, node_map)
                        
                    if self.options.write_cur_maps:
                        cs.accumulate_c_map(vc_map_id, voltages, G, node_map, finitegrounds, None, None)
        
        if solver_failed==False:
            cs.write_v_map(vc_map_id)
            
        if ((source_id == None) and self.options.write_cur_maps) or ((source_id != None) and (self.options.write_cum_cur_map_only==False)):
            cs.write_c_map(vc_map_id)
            
        if self.options.scenario=='one-to-all':
            if solver_failed==False:
                (row, col) = np.where(source_map>0)
                vmap = cs.get_v_map(vc_map_id)
                voltages = vmap[row,col]/source_map[row,col] #allows for variable source strength
        elif self.options.scenario=='all-to-one':
            if solver_failed==False:
                voltages = 0 #return 0 voltage/resistance for all-to-one mode           

        cs.rm_v_map(vc_map_id)
        # Advanced mode will return voltages of the last component solved only for verification purposes.  
        if solver_called==False:
            voltages = -1

        return voltages, cs.get_c_map(vc_map_id, True), solver_failed

        
    def resolve_conflicts(self, sources, grounds):
        """Handles conflicting grounds and sources for advanced mode according to user preferences."""  
        finitegrounds = np.where(grounds < np.Inf, grounds, 0)
        if (np.where(finitegrounds==0, 0, 1)).sum()==0:
            finitegrounds = [-9999]
        infgrounds = np.where(grounds==np.Inf, 1, 0)
        
        ##Resolve conflicts bewteen sources and grounds
        conflicts = np.logical_and(sources, grounds)
        if self.options.remove_src_or_gnd == 'rmvsrc':
            sources = np.where(conflicts, 0, sources)
        elif self.options.remove_src_or_gnd == 'rmvgnd':
            grounds = np.where(conflicts, 0, grounds)
        elif self.options.remove_src_or_gnd == 'rmvall':
            sources = np.where(conflicts, 0, sources)
        infconflicts = np.logical_and(sources, infgrounds)
        grounds = np.where(infconflicts, 0, grounds)
        if np.size(np.where(sources)) == 0:
            raise RuntimeError('All sources conflicted with grounds and were removed. There is nothing to solve.') 
        if np.size(np.where(grounds)) == 0:
            raise RuntimeError('All grounds conflicted with sources and were removed.  There is nothing to solve.') 

        return (sources, grounds, finitegrounds)


    @print_rusage
    def multiple_solver(self, G, sources, grounds, finitegrounds):
        """Solver used for advanced mode."""  
        if finitegrounds[0]==-9999:#Fixme: no need to do this, right?
            finitegrounds = np.zeros(G.shape[0], dtype='int32') #create dummy vector for pairwise case
            Gsolve = G + sparse.spdiags(finitegrounds.T, 0, G.shape[0], G.shape[0]) 
            finitegrounds = [-9999]
        else:
            Gsolve = G + sparse.spdiags(finitegrounds.T, 0, G.shape[0], G.shape[0]) 
           
        ##remove infinite grounds from graph
        infgroundlist = np.where(grounds==np.Inf)
        infgroundlist = infgroundlist[0]
        numinfgrounds = infgroundlist.shape[0]
        
        dst_to_delete = []
        for ground in range(1, numinfgrounds+1):
            dst = infgroundlist[numinfgrounds-ground]
            dst_to_delete.append(dst)
            #Gsolve = deleterowcol(Gsolve, delrow = dst, delcol = dst)
            keep = np.delete(np.arange(0, sources.shape[0]), dst)
            sources = sources[keep]            
        Gsolve = ComputeBase.deleterowcol(Gsolve, delrow = dst_to_delete, delcol = dst_to_delete)
        
        self.state.create_amg_hierarchy(Gsolve, self.options.solver)
        voltages = ComputeBase.solve_linear_system(Gsolve, sources, self.options.solver, self.state.amg_hierarchy)
        del Gsolve
        self.state.del_amg_hierarchy()

        numinfgrounds = infgroundlist.shape[0]
        if numinfgrounds>0:
            #replace infinite grounds in voltage vector
            for ground in range(numinfgrounds,0, -1): 
                node = infgroundlist[numinfgrounds - ground] 
                voltages = np.asmatrix(np.insert(voltages,node,0)).T
        return np.asarray(voltages).reshape(voltages.size)
            

    @print_rusage
    def create_voltage_map(self, node_map, voltages):
        """Creates raster map of voltages given node voltage vector."""
        voltage_map = np.zeros((self.state.nrows, self.state.ncols), dtype = 'float64')
        ind = node_map > 0
        voltage_map[np.where(ind)] = np.asarray(voltages[node_map[ind]-1]).flatten()
        return voltage_map
            
 
    def get_voltmatrix(self, i, j, numpoints, local_node_map, voltages, fp, resistances, voltmatrix):                                            
        """Returns a matrix of pairwise voltage differences between focal nodes.
        
        Used for shortcut calculations of effective resistance when no
        voltages or currents are mapped.
        
        """  
        voltvector = np.zeros((numpoints,1),dtype = 'float64')  
        voltage_map = self.create_voltage_map(local_node_map,voltages) 
        for point in range(1,numpoints):
            voltageAtPoint = voltage_map[fp.get_coordinates(point)]
            voltageAtPoint = 1-(voltageAtPoint/resistances[i, j])
            voltvector[point] = voltageAtPoint
        voltmatrix[:,j] = voltvector[:,0] 


    @staticmethod
    def get_shortcut_resistances(anchor_point, voltmatrix, numpoints, resistances, shortcut_resistances): #FIXME: no solver failed capability
        """Calculates all resistances to each focal node at once.
        
        Greatly speeds up resistance calculations if not mapping currents or voltages.
        
        """  
        for pointx in range(0, numpoints): #anchor_point is source node, i.e. the 1 in R12.  point 2 is the dst node.
            R1x = resistances[anchor_point, pointx]
            if R1x!= -1:
                shortcut_resistances[pointx, anchor_point] = shortcut_resistances[anchor_point, pointx] = R1x
                for point2 in range(pointx, numpoints):
                    R12 = resistances[anchor_point, point2] 
                    if R12!= -1:
                        shortcut_resistances[anchor_point, point2] = shortcut_resistances[point2, anchor_point] = R12
                        Vx = voltmatrix[pointx, point2]
                        R2x = 2*R12*Vx + R1x - R12
                        shortcut_resistances[point2, pointx] = shortcut_resistances[pointx, point2] = R2x


    def write_resistances(self, point_ids, resistances):
        """Writes resistance file to disk."""  
        outputResistances = self.append_names_to_resistances(point_ids, resistances)
        resistances3Columns = self.convertResistances3cols(outputResistances)
        CSIO.write_resistances(self.options.output_file, outputResistances, resistances3Columns)
        return outputResistances, resistances3Columns
        
    def convertResistances3cols(self, resistances):
        """Converts resistances from matrix to 3-column format."""  
        numPoints = resistances.shape[0]-1
        numEntries = numPoints*(numPoints-1)/2
        resistances3columns = np.zeros((numEntries,3),dtype = 'float64') 
        x = 0
        for i in range(1,numPoints):
            for j in range(i+1,numPoints+1):
                resistances3columns[x,0] = resistances[i,0]    
                resistances3columns[x,1] = resistances[0,j]
                resistances3columns[x,2] = resistances[i,j]
                x = x+1
        return resistances3columns        
        
    
        
    def get_strength_map(self, points_rc, point_strengths):
        """Returns map and coordinates of point strengths when variable source strengths are used."""  
        if self.options.use_variable_source_strengths==True:
            strengths_rc = self.get_strengths_rc(points_rc, point_strengths)
            if self.options.scenario == 'one-to-all': 
                strength_map = None
            else:
                strength_map = np.zeros((self.state.nrows, self.state.ncols), dtype='Float64')
                strength_map[points_rc[:,1], points_rc[:,2]] = strengths_rc[:,0]     
            return strength_map,strengths_rc
        else:
            return None,None
        
    @staticmethod
    def get_strengths_rc(points_rc, point_strengths):
        """Returns coordinates of point strengths when variable source strengths are used."""  
        strength_ids = list(point_strengths[:,0])
        strength_values = list(point_strengths[:,1])
        
        strengths_rc = np.zeros(points_rc.shape, dtype='float64')
        strengths_rc[:,1] = points_rc[:,1]
        strengths_rc[:,2] = points_rc[:,2]
        for point in range(0, points_rc.shape[0]):
            try:
                point_id = points_rc[point,0]
                indx = strength_ids.index(point_id)
                strengths_rc[point,0] = strength_values[indx]
            except ValueError:
                strengths_rc[point,0] = 1
        return strengths_rc        


    @gc_after
    @print_rusage
    def load_maps(self):
        """Loads all raster maps into self.state."""  
        Compute.logger.info('Reading maps')
        reclass_file = self.options.reclass_file if self.options.use_reclass_table else None
        CSIO.read_cell_map(self.options.habitat_file, self.options.habitat_map_is_resistances, reclass_file, self.state)
        
        if self.options.use_polygons:
            self.state.poly_map = CSIO.read_poly_map(self.options.polygon_file, False, 0, self.state, True, "Short-circuit region", 'int32')
        else:
            self.state.poly_map = []
 
        if self.options.use_mask==True:
            mask = CSIO.read_poly_map(self.options.mask_file, True, 0, self.state, True, "Mask", 'int32')
            mask = np.where(mask !=  0, 1, 0) 
            self.state.g_map = np.multiply(self.state.g_map, mask)
            del mask
            
            sum_gmap = (self.state.g_map).sum()
            if sum_gmap==0:
                raise RuntimeError('All entries in habitat map have been dropped after masking with the mask file.  There is nothing to solve.')             
        else:
            self.state.mask = []

        if self.options.scenario=='advanced':
            self.state.points_rc = []
            (self.state.source_map, self.state.ground_map) = CSIO.read_source_and_ground_maps(self.options.source_file, self.options.ground_file, self.state, self.options)
        else:        
            self.state.points_rc = CSIO.read_point_map(self.options.point_file, "Focal node", self.state)
            self.state.source_map = []
            self.state.ground_map = []

        if self.options.use_included_pairs==True:
            self.state.included_pairs = CSIO.read_included_pairs(self.options.included_pairs_file)
        
        self.state.point_strengths = None
        if self.options.use_variable_source_strengths==True:
            self.state.point_strengths = CSIO.read_point_strengths(self.options.variable_source_file) 
        
        Compute.logger.info('Processing maps')
        return 
 

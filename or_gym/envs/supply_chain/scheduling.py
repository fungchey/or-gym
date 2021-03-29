import numpy as np
import gym
from gym import spaces
from abc import ABC, abstractmethod
from collections import namedtuple, deque, OrderedDict
from collections.abc import Iterable
from operator import attrgetter
from or_gym import utils

class BaseSchedEnv(gym.Env, ABC):
    '''
    Base scheduling environment. Additional models can be built on 
    top of this class.

    Parameters:
        time_limit: int, number of hours in the episode.
        n_stages: int, number of processing stages.
        n_fin_products: int, number of finished products that can be sold.
        n_int_products: int, number of intermediate products that are 
            converted into finished products.
        product_ids: array, identifier for each product.
        _run_rate: int, placeholder value for units produced per hour.
        run_rates: dict, maps products to run rate to allow for variation. 
            Uses _run_rate by default.
        _product_value: int, default value of each units of product.
        product_values: dict, maps products to their values. Uses 
            _product_value by default.
        ship_by_time: int, time to ship orders each day. If orders are due on
            a given day, but material isn't available to ship them, they will
            be marked late and penalties may be applied.
        init_inventory: array, initial inventory to begin each episode.
        _cure_time: int, hours a product must sit (cool, degas, etc.) before
            it can be shipped or processed by the next stage.
        cure_times: dict, maps products to cure times. Uses _cure_time value
            for all products by default.
        _holding_cost: int, unit cost for holding inventory.
        holding_costs: dict, maps products to specific holding costs. Uses
            _holding_cost by default.
        _converstion_rate: float, used to convert input products to output
            quantities.
        conversion_rates: dict, converts input products to output quantities.
            Uses _conversion_rate by default.
        order_qty: int, fixed quantity for orders.
        min_schedule_length: int, minimum time the schedule needs to be
            maintained in hours.
    '''
    def __init__(self, *args, **kwargs):
        self.simulation_days = 365
        self.time_limit = self.simulation_days * 24 # Hours
        self.n_fin_products = 10 # Finished products
        self.n_int_products = 0  # Intermediate products
        self.product_ids = np.arange(self.n_fin_products + 
            self.n_int_products)
        self.init_inventory = np.ones(self.n_fin_products + 
            self.n_int_products) * 100
        self.n_stages = 1
        self._run_rate = 10 # Units/hour
        self._product_value = 10 # $/Unit
        self._min_production_qty = 100 # Units
        self.ship_by_time = 24 # Orders ship by midnight each day
        self._cure_time = 24 # Hours
        self._holding_cost = 1 # $/Units
        self._conversion_rate = 1 # Applicable for multi-stage models
        self.avg_lead_time = 7 # Days
        self.min_schedule_length = 7 * 24 # Hours
        
        self.run_rates = {i: self._run_rate 
            for i in self.product_ids}
        self.product_values = {i: self._product_value
            for i in self.product_ids}
        self.min_product_qtys = {i: self._min_production_qty
            for i in self.product_ids}
        self.cure_times = {i: self._cure_time 
            for i in self.product_ids}
        self.holding_costs = {i: self._holding_cost
            for i in self.product_ids}
        self.conversion_rates = {i: self._conversion_rate
            for i in self.product_ids}
        self.prod_inv_idx = {j: i 
            for i, j in enumerate(self.product_ids)}

        self.order_book_cols = ['Num', 'ProductID', 'CreateDate', 'DueDate', 
            'Value', 'Shipped', 'ShipDate', 'OnTime']
        self.ob_col_idx = {j: i for i, j in enumerate(self.order_book_cols)}

        self.sched_cols = ['Num', 'ProductID', 'Stage', 'Line', 
            'StartTime', 'EndTime', 'ReleaseTime', 'Quantity', 
            'OffGrade', 'Completed']
        self.sched_col_idx = {j: i for i, j in enumerate(self.sched_cols)}

        self._check_unique_prods()
        self._init_demand = False
        self._initialize_demand_model()

    def _check_unique_prods(self):
        # Product IDs must be unique.
        _, count = np.unique(self.product_ids, return_counts=True)
        assert count.max() == 1, "Non-unique products found: {}".format(
            self.product_ids[np.where(count>1)])

    def _STEP(self, action):
        if not isinstance(action, Iterable):
            action = np.array([action])

        done = False
        reward = 0
        self.append_schedules(action)
        # Get next prod end time and the latest prod start time from scheds
        self._next_end_time = self._get_next_end_time()
        self._latest_start_time = self._get_latest_start_time()
        
        # While the schedule extends beyond the minimum  schedule length,
        # the environment pushes new product through the reactors, updates
        # inventory, and ships orders.
        while self._next_end_time > self.env_time + self.min_schedule_length:
            if len(self.production_start_deque) < self.action_inputs:
                self.production_start_deque = self._get_next_production_starts()
            
            # Begin production
            try:
                while self.production_start_deque[0].ProdStartTime == self.env_time:
                    self.start_production(self.production_deque[0])
                    self.production_deque.popleft()
            except IndexError:
                pass
            self.production_release_deque = deque(
                sorted(self.production_release_deque, 
                key=attrgetter('ProdReleaseTime')))
            
            # Release products to inventory
            try:
                while self.production_release_deque[0].ProdReleaseTime == self.env_time:
                    self.book_inventory(self.production_release_deque[0])
                    self.production_release_deque.popleft()
            except IndexError:
                pass
            
            # Ship orders to fulfill demand
            if self.env_time % sef.ship_by_time == 0:
                self.ship_orders()
            if self.run_maintentance_model():
                break

            # Get totals every 24-hours
            if self.env_time % 24 == 0:
                pass

            self.env_time += 1

            if self.env_time >= self.time_limit:
                done = True
                break
        
        self.state = self.get_state()
        info = {}
        return self.state, reward, done, info

    def _RESET(self):
        # Deque of products to release
        self.production_deque = deque()
        self.inventory = self.init_inventory.copy()
        self.env_time = 0
        self.order_book = self.get_demand()
        return self.get_state()

    def _calculate_reward(self):
        pass

    def _get_state(self):
        pass

    def _initialize_demand_model(self):
        # Only initialize at beginning of model
        if self._init_demand:
            return None
        self.mean_total_demand = np.mean([i
            for i in self.run_rates.values()]) * self.time_limit
        
        self.product_demand = self._get_product_demand()
        self._seasonal_offset = np.random.rand() * 2 * np.pi
        _sin = np.sin(np.linspace(0, 2*np.pi, self.simulation_days)
            + self._seasonal_offset) + 1
        self._p_seas = _sin / _sin.sum()
        self._init_demand = True

    def _get_product_demand(self):
        # Randomly provides a percentage of total demand to each finished
        # product.
        s = np.random.normal(size=self.n_fin_products)
        shares = utils.softmax(s)
        return np.round(shares * self.mean_total_demand, 0).astype(int)

    def _run_demand_model(self):
        '''
        Base model calculates mean run rate for the products, multiplies
        this by the number of hours in the simulation, and uses this as
        the mean, total demand for the each episode. A fraction of the
        total demand is then split among the various final products. Time
        series are made by sampling orders from a normalized sine wave to
        simulate seasonality. Random values (e.g. demand shares) are
        fixed whenever or_gym.make() is called, and are preserved during each
        call to reset().
        Returns order_book object containing demand data for the episode.
        '''
        self._initialize_demand_model()
        order_book = np.zeros((self.product_demand.sum(), 
            len(self.order_book_cols)))
        order_book[:, self.ob_col_idx['Num']] = np.arange(
            self.product_demand.sum())
        prods = np.hstack([np.repeat(i, j)
            for i, j in zip(self.product_ids, self.product_demand)])
        order_book[:, self.ob_col_idx['ProductID']] = prods
        due_dates = np.hstack([np.random.choice(np.arange(0, self.simulation_days),
            p=self._p_seas, size=i)
            for i in self.product_demand])
        order_book[:, self.ob_col_idx['DueDate']] = due_dates
        order_book[:, self.ob_col_idx['CreateDate']] = due_dates - \
            np.random.poisson(lam=self.avg_lead_time, 
                size=self.product_demand.sum())

        return order_book

    def _maintentance_model(self):
        return False

    def run_maintentance_model(self):
        return self._maintentance_model()

    def start_production(self, prod_tuple):
        pass

    def book_inventory(self, prod_tuple):
        '''
        Moves completed production into inventory.
        '''
        prod_idx = self.prod_inv_idx[prod_tuple.ProdID]
        self.inventory[prod_idx] += prod_tuple.Quantity
        self._mark_booked_in_schedule(prod_tuple)

    def _mark_booked_in_schedule(self, prod_tuple):
        # Changes booked column from 0 to 1
        sched = self.schedules[prod_tuple.Stage][prod_tuple.Line]
        row_to_book = np.where(
            sched[:, self.sched_idx['Num']]==prod_tuple.Number)
        sched[row_to_book, self.sched_idx['Booked']] = 1
        sched[row_to_book, self.sched_idx['Quantity']] = prod_tuple.Quantity
        self.schedules[prod_tuple.Stage][prod_tuple.Line] = sched.copy()

    def append_schedules(self, action):
        # Sampling passes actions as OrderedDict
        # if type(action) is OrderedDict:
            # action = action.values()
        # Ray passes actions as dictionary values
        if type(action) is dict or type(action) is OrderedDict:
            for k, v in action.items():
                stage, train = k.split('_')
                schedule = self.schedules[stage][train]
                self._append_schedule(v, stage, train, schedule)
        else:
            for a, (stage, train, _) in zip(action, self.stage_train_list):
                schedule = self.schedules[stage][train]
                self._append_schedule(a, stage, train, schedule)
            
    def _append_schedule(self, action, stage, train, schedule):
        stage_num = utils.get_digits(stage)
        train_num = utils.get_digits(train)

        # Get values from mappings
        gmid = self.map_action_to_gmid(stage, train, action, stage_num)
        if gmid is None:
            return None
        batch_size = self.get_batch_size(stage, train, gmid)
        booked = 0
        if schedule is None:
            num = 1
            start_time = self.sim_time
            last_gmid = 0 # Map to startup
        else:
            last_gmid = schedule[-1, self.sched_idx['ProdID']]
            if gmid == last_gmid:
                # TODO: Extend current entry by some discrete amount
                # be it minimum batch size or some other value.
                pass
            num = schedule[-1, self.sched_idx['Num']] + 1
            start_time = schedule[-1, self.sched_idx['ProdEndTime']]  
        
        off_grade = self.get_off_grade(stage, train, gmid, last_gmid)
        if (gmid == 0 or gmid == '0') and stage_num > 0:
            end_time = start_time + self.wait_time
            release_time = end_time
        else:
            try:
                end_time = np.ceil(start_time + 
                    (batch_size + off_grade) / self.get_production_rate(
                        stage, train, gmid))
            except ZeroDivisionError:
                # Occurs in some test cases without action masking, 
                # set end_time = start_time...I think...
                end_time = start_time
                # print("stage: {}\ttrain: {}\tgmid: {}".format(
                #     stage, train, gmid))
            release_time = end_time + self.get_cure_time(stage, train, gmid)
        
        new_entry = np.zeros(len(self.sched_idx)) # Next line in schedule
        # Add values to new_entry
        new_entry[self.sched_idx['Stage']] = stage_num
        new_entry[self.sched_idx['Line']] = line_num
        new_entry[self.sched_idx['Num']] = num
        new_entry[self.sched_idx['ProdID']] = prod_id
        new_entry[self.sched_idx['Quantity']] = qty
        new_entry[self.sched_idx['StartTime']] = start_time
        new_entry[self.sched_idx['EndTime']] = end_time
        new_entry[self.sched_idx['ReleaseTime']] = release_time
        new_entry[self.sched_idx['OffGrade']] = off_grade
        new_entry[self.sched_idx['Completed']] = completed
        
        if schedule is None:
            self.schedules[stage][train] = new_entry.reshape(1, -1).copy().astype(int)
        else:
            self.schedules[stage][train] = np.vstack([schedule, new_entry.astype(int)])

    def get_demand(self):
        return self._run_demand_model()

    def get_state(self):
        return self._get_state()

    @abstractmethod
    def step(self, action):
        raise NotImplementedError("step() method not implemented.")

    @abstractmethod
    def reset(self):
        raise NotImplementedError("reset() method not implemented.")

class SingleStageSchedEnv(BaseSchedEnv):
    '''
    This is the simplest scheduling environment where the agent needs to 
    manage a single production line with fixed production sizes and rates 
    to meet demand.

    The only action available to the agent is what product to produce next.
    This selection is appended to the end of the schedule and production will
    begin on this next product when it is scheduled.

    Actions:
        Type: Discrete
        0: Produce product 0
        1: Produce product 1
        2: ... 

    Observations:
        Type: Dictionary
        production_state:
            Type: Box
            0: Current time
            1: Schedule time
            2: Inventory of product 0
            3: Inventory of product 1
            4: ... 
        demand_state:
            Type: Box
        forecast_state:
            Type: Box
    '''
    def __init__(self, *args, **kwargs):
        utils.assign_env_config(self, kwargs)
        super().__init__()

        self._stage_line_dict = {0: 
            {0: self.product_ids}
        }

        self.action_space = spaces.Dict({k: 
            spaces.Dict({k1: spaces.Discrete(len(v1)) 
                for k1, v1 in v.items()})
            for k, v in _stage_line_dict.items()})

        self.observation_space = None

        self.reset()

    def step(self, action):
        return self._STEP(action)

    def reset(self):
        return self._RESET()
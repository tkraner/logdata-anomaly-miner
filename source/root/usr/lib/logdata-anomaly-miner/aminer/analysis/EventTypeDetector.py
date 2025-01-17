"""
This module can assigns every parsed log line a eventtype and can be used for profiling purposes.
It supports the modules VariableTypeDetector and VariableCorrelationDetector.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""
import time
import logging

from aminer import AminerConfig
from aminer.AminerConfig import build_persistence_file_name, KEY_PERSISTENCE_PERIOD, DEFAULT_PERSISTENCE_PERIOD, DEBUG_LOG_NAME
from aminer.AnalysisChild import AnalysisContext
from aminer.input.InputInterfaces import AtomHandlerInterface
from aminer.util.TimeTriggeredComponentInterface import TimeTriggeredComponentInterface
from aminer.util import PersistenceUtil


class EventTypeDetector(AtomHandlerInterface, TimeTriggeredComponentInterface):
    """This class keeps track of the found event types and the values of each variable."""

    time_trigger_class = AnalysisContext.TIME_TRIGGER_CLASS_REALTIME

    def __init__(self, aminer_config, anomaly_event_handlers, persistence_id='Default', target_path_list=None, id_path_list=None,
                 allow_missing_id=False, allowed_id_tuples=None, min_num_vals=1000, max_num_vals=1500, save_values=True):
        """
        Initialize the detector. This will also trigger reading or creation of persistence storage location.
        @param aminer_config configuration from analysis_context.
        @param anomaly_event_handlers for handling events, e.g., print events to stdout.
        @param persistence_id name of persistence file.
        @param target_path_list parser paths of values to be analyzed. Multiple paths mean that all values occurring in these paths are
               considered for value range generation.
        @param id_path_list specifies group identifiers for which data should be learned/analyzed. One or more paths that specify the trace
               of the sequence detection, i.e., incorrect sequences that are generated by interleaved events can be avoided when event
               sequence identifiers are available (list of strings, defaults to empty list).
        @param allow_missing_id specifies whether log atoms without id path should be omitted (only if id path is set).
        @param min_num_vals number of the values which the list of stored logline values is being reduced to.
        @param max_num_vals the maximum list size of the stored logline values before being reduced to the last min_num_values.
        @param save_values if false the values of the log atom are not saved for further analysis. This disables values and check_variables.
        """
        # avoid "defined outside init" issue
        self.next_persist_time, self.log_success, self.log_total = [None]*3
        super().__init__(
            mutable_default_args=["id_path_list"], aminer_config=aminer_config,
            anomaly_event_handlers=anomaly_event_handlers, persistence_id=persistence_id, target_path_list=target_path_list,
            id_path_list=id_path_list, allow_missing_id=allow_missing_id, allowed_id_tuples=allowed_id_tuples, min_num_vals=min_num_vals,
            max_num_vals=max_num_vals, save_values=save_values
        )

        self.num_events = 0
        self.longest_path = []  # List of the longest path of the events
        self.found_keys = []  # List of the keys corresponding to the events
        self.variable_key_list = []  # List of the keys, which take values in the log line
        # List of the values of the log lines. If the length reaches max_num_vals the list gets reduced to min_num_vals values per variable
        self.values = []
        self.num_event_lines = []  # Saves the number of lines of the event types
        self.total_records = 0  # Saves the number of total log lines
        # List of the modules which follow the event_type_detector. The implemented modules are form the list
        # [VariableTypeDetector, VariableCorrelationDetector, TSAArimaDetector]
        self.following_modules = []
        self.check_variables = []  # List of bools, which state if the variables of variable_key_list are updated.
        # List ot the time trigger. The first list states the times when something should be triggered, the second list states the indices
        # of the event types, or a list of the event type, a path and a value which should be counted (-1 for an initialization)
        # the third list states, the length of the time step (-1 for a one time trigger)
        self.etd_time_trigger = [[], [], []]
        self.num_event_lines_tsa_ref = []  # Reference containing the number of lines of the events for the TSA
        self.current_index = 0  # Index of the event type of the current log line
        self.id_path_list_tuples = []  # List of the id tuples

        # Loads the persistence
        self.persistence_file_name = build_persistence_file_name(aminer_config, self.__class__.__name__, persistence_id)
        PersistenceUtil.add_persistable_component(self)
        persistence_data = PersistenceUtil.load_json(self.persistence_file_name)

        # Imports the persistence
        if persistence_data is not None:
            for key in persistence_data[0]:
                self.found_keys.append(set(key))
            self.variable_key_list = persistence_data[1]
            self.values = persistence_data[2]
            self.longest_path = persistence_data[3]
            self.check_variables = persistence_data[4]
            self.num_event_lines = persistence_data[5]
            self.id_path_list_tuples = [tuple(tuple_list) for tuple_list in persistence_data[6]]

            self.num_events = len(self.found_keys)

    def receive_atom(self, log_atom):
        """Receives a parsed atom and keeps track of the event types and the values of the variables of them."""
        self.log_total += 1
        valid_log_atom = False
        if self.target_path_list:
            for path in self.target_path_list:
                if path in log_atom.parser_match.get_match_dictionary().keys():
                    valid_log_atom = True
                    break
        if self.target_path_list and not valid_log_atom:
            self.current_index = -1
            return False
        self.total_records += 1

        # Get the current index, either from the combination of values of the paths of id_path_list, or the event type
        if self.id_path_list:
            # In case that id_path_list is set, use it to differentiate sequences by their id.
            # Otherwise, the empty tuple () is used as the only key of the current_sequences dict.
            id_tuple = ()
            for id_path in self.id_path_list:
                id_match = log_atom.parser_match.get_match_dictionary().get(id_path)
                if id_match is None:
                    if self.allow_missing_id is True:
                        # Insert placeholder for id_path that is not available
                        id_tuple += ('',)
                    else:
                        # Omit log atom if one of the id paths is not found.
                        return False
                else:
                    if isinstance(id_match.match_object, bytes):
                        id_tuple += (id_match.match_object.decode(AminerConfig.ENCODING),)
                    else:
                        id_tuple += (id_match.match_object,)

            # Check if only certain tuples are allowed and if the tuple is included.
            if self.allowed_id_tuples != [] and id_tuple not in self.allowed_id_tuples:
                self.current_index = -1
                return False

            # Searches if the id_tuple has previously appeared
            current_index = -1
            for event_index, var_key in enumerate(self.id_path_list_tuples):
                if id_tuple == var_key:
                    current_index = event_index
        else:
            # Searches if the event type has previously appeared
            current_index = -1
            for event_index in range(self.num_events):
                if self.longest_path[event_index] in log_atom.parser_match.get_match_dictionary() and set(
                        log_atom.parser_match.get_match_dictionary()) == self.found_keys[event_index]:
                    current_index = event_index

        # Initialize a new event type if the event type of the new line has not appeared
        if current_index == -1:
            current_index = self.num_events
            self.num_events += 1
            self.found_keys.append(set(log_atom.parser_match.get_match_dictionary().keys()))

            # Initialize the list of the keys to the variables
            self.variable_key_list.append(list(self.found_keys[current_index]))
            # Delete the entries with value None or timestamps as values
            for var_index in range(len(self.variable_key_list[current_index]) - 1, -1, -1):
                if (type(log_atom.parser_match.get_match_dictionary()[self.variable_key_list[current_index][var_index]]).__name__ !=
                        'MatchElement') or (log_atom.parser_match.get_match_dictionary()[self.variable_key_list[
                        current_index][var_index]].match_object is None):
                    del self.variable_key_list[current_index][var_index]
                elif (self.target_path_list is not None) and self.variable_key_list[current_index][var_index] not in self.target_path_list:
                    del self.variable_key_list[current_index][var_index]

            # Initialize the empty lists for the values and initialize the check_variables list for the variables
            if self.save_values:
                self.init_values(current_index)
                self.check_variables.append([True for _ in range(len(self.variable_key_list[current_index]))])
            self.num_event_lines.append(0)

            if not self.id_path_list:
                # String of the longest found path
                self.longest_path.append('')
                # Number of forward slashes in the longest path
                tmp_int = 0
                if self.target_path_list is None:
                    for var_key in self.variable_key_list[current_index]:
                        if var_key is not None:
                            count = var_key.count('/')
                            if count > tmp_int or (count == tmp_int and len(self.longest_path[current_index]) < len(var_key)):
                                self.longest_path[current_index] = var_key
                                tmp_int = count
                else:
                    for found_key in list(self.found_keys[current_index]):
                        if found_key is None:
                            found_key = ""
                        count = found_key.count('/')
                        if count > tmp_int or (count == tmp_int and len(self.longest_path[current_index]) < len(found_key)):
                            self.longest_path[current_index] = found_key
                            tmp_int = count
            else:
                self.id_path_list_tuples.append(id_tuple)
        self.current_index = current_index

        if self.save_values:
            # Appends the values to the event type
            self.append_values(log_atom, current_index)
        self.num_event_lines[current_index] += 1
        self.log_success += 1
        return True

    def do_timer(self, trigger_time):
        """Check if current ruleset should be persisted."""
        if self.next_persist_time is None:
            return self.aminer_config.config_properties.get(KEY_PERSISTENCE_PERIOD, DEFAULT_PERSISTENCE_PERIOD)

        delta = self.next_persist_time - trigger_time
        if delta <= 0:
            self.do_persist()
            delta = self.aminer_config.config_properties.get(KEY_PERSISTENCE_PERIOD, DEFAULT_PERSISTENCE_PERIOD)
            self.next_persist_time = time.time() + delta
        return delta

    def do_persist(self):
        """Immediately write persistence data to storage."""
        tmp_list = [[]]
        for key in self.found_keys:
            tmp_list[0].append(list(key))
        tmp_list.append(self.variable_key_list)
        tmp_list.append(self.values)
        tmp_list.append(self.longest_path)
        tmp_list.append(self.check_variables)
        tmp_list.append(self.num_event_lines)
        tmp_list.append(self.id_path_list_tuples)
        PersistenceUtil.store_json(self.persistence_file_name, tmp_list)

        logging.getLogger(DEBUG_LOG_NAME).debug(f'{self.__class__.__name__} persisted data.')

    def add_following_modules(self, following_module):
        """Add the given Module to the following module list."""
        self.following_modules.append(following_module)
        logging.getLogger(DEBUG_LOG_NAME).debug(
            f'{self.__class__.__name__} added following module {following_module.__class__.__name__}.')

    def init_values(self, current_index):
        """Initialize the variable_key_list and the list for the values."""
        # Initializes the value list
        if not self.values:
            self.values = [[[] for _ in range(len(self.variable_key_list[current_index]))]]
        else:
            self.values.append([[] for _ in range(len(self.variable_key_list[current_index]))])

    def append_values(self, log_atom, current_index):
        """Add the values of the variables of the current line to self.values."""
        for var_index, var_key in enumerate(self.variable_key_list[current_index]):
            # Skips the variable if check_variable is False, or if the var_key is not included in the match_dict
            if not self.check_variables[current_index][var_index]:
                continue
            if var_key not in log_atom.parser_match.get_match_dictionary():
                self.values[current_index][var_index] = []
                self.check_variables[current_index][var_index] = False
                continue

            raw_match_object = ''
            if isinstance(log_atom.parser_match.get_match_dictionary()[var_key].match_object, bytearray):
                raw_match_object = repr(
                    bytes(log_atom.parser_match.get_match_dictionary()[var_key].match_object))[2:-1]
            elif isinstance(log_atom.parser_match.get_match_dictionary()[var_key].match_object, bytes):
                raw_match_object = repr(log_atom.parser_match.get_match_dictionary()[var_key].match_object)[2:-1]

            # Try to convert the values to floats and add them as values
            try:
                if raw_match_object != '':
                    self.values[current_index][var_index].append(float(raw_match_object))
                else:
                    self.values[current_index][var_index].append(
                        float(log_atom.parser_match.get_match_dictionary()[var_key].match_object))
            # Add the strings as values
            except:  # skipcq: FLK-E722
                if isinstance(log_atom.parser_match.get_match_dictionary()[var_key].match_string, bytes):
                    self.values[current_index][var_index].append(
                        repr(log_atom.parser_match.get_match_dictionary()[var_key].match_string)[2:-1])
                else:
                    self.values[current_index][var_index].append(log_atom.parser_match.get_match_dictionary()[var_key].match_string)

        # Reduce the numbers of entries in the value list
        if len(self.variable_key_list[current_index]) > 0 and len([i for i in self.check_variables[current_index] if i]) > 0 and \
                len(self.values[current_index][self.check_variables[current_index].index(True)]) > self.max_num_vals:
            for var_index in range(len(self.variable_key_list[current_index])):  # skipcq: PTC-W0060
                # Skips the variable if check_variable is False
                if not self.check_variables[current_index][var_index]:
                    continue
                self.values[current_index][var_index] = self.values[current_index][var_index][-self.min_num_vals:]

    def get_event_type(self, event_index):
        """Return a string which includes information about the event type."""
        if self.id_path_list:
            return_string = str(event_index) + '(' + str(self.id_path_list_tuples[event_index]) + ')'
        else:
            return_string = str(event_index) + '(' + str(self.longest_path[event_index]) + ')'
        return return_string

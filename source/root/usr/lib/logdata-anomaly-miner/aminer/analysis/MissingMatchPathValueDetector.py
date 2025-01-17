"""
This module provides the MissingMatchPathValueDetector to generate events when expected values were not seen for an extended period of time.
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

from aminer.AminerConfig import build_persistence_file_name, DEBUG_LOG_NAME, KEY_PERSISTENCE_PERIOD, DEFAULT_PERSISTENCE_PERIOD,\
    STAT_LOG_NAME
from aminer import AminerConfig
from aminer.AnalysisChild import AnalysisContext
from aminer.events.EventInterfaces import EventSourceInterface
from aminer.input.InputInterfaces import AtomHandlerInterface
from aminer.util import PersistenceUtil
from aminer.util.TimeTriggeredComponentInterface import TimeTriggeredComponentInterface


class MissingMatchPathValueDetector(AtomHandlerInterface, TimeTriggeredComponentInterface, EventSourceInterface):
    """
    This class creates events when an expected value is not seen within a given timespan.
    For example because the service was deactivated or logging disabled unexpectedly. This is complementary to the function provided by
    NewMatchPathValueDetector. For each unique value extracted by paths, a tracking record is added to expected_values_dict.
    It stores three numbers: the timestamp the extracted value was last seen, the maximum allowed gap between observations and the next
    alerting time when currently in error state. When in normal (alerting) state, the value is zero.
    """

    time_trigger_class = AnalysisContext.TIME_TRIGGER_CLASS_REALTIME

    def __init__(self, aminer_config, target_path_list, anomaly_event_handlers, persistence_id='Default', learn_mode=False,
                 default_interval=3600, realert_interval=86400, combine_values=True, output_logline=True, stop_learning_time=None,
                 stop_learning_no_anomaly_time=None):
        """
        Initialize the detector. This will also trigger reading or creation of persistence storage location.
        @param aminer_config configuration from analysis_context.
        @param target_path_list parser paths of values to be analyzed. Multiple paths mean that all values occurring in these paths are
               considered for value range generation.
        @param anomaly_event_handlers for handling events, e.g., print events to stdout.
        @param persistence_id name of persistence file.
        @param learn_mode specifies whether new expected values should be learned.
        @param default_interval time in seconds before a value is reported missing. The parameter is applied to the parsed record data time,
               not the system time. Hence, reports can be delayed when no data is received.
        @param realert_interval time in seconds before a value is reported missing for a second time. The parameter is applied to the
               parsed record data time, not the system time. Hence, reports can be delayed when no data is received.
        @param combine_values if true the combined values are used as identifiers. When false, individual values are checked.
        @param stop_learning_time switch the learn_mode to False after the time.
        @param stop_learning_no_anomaly_time switch the learn_mode to False after no anomaly was detected for that time.
        """
        # avoid "defined outside init" issue
        self.learn_mode, self.stop_learning_timestamp, self.next_persist_time, self.log_success, self.log_total = [None]*5
        super().__init__(
            aminer_config=aminer_config, target_path_list=target_path_list, anomaly_event_handlers=anomaly_event_handlers,
            persistence_id=persistence_id, learn_mode=learn_mode, default_interval=default_interval, realert_interval=realert_interval,
            output_logline=output_logline, combine_values=combine_values, stop_learning_time=stop_learning_time,
            stop_learning_no_anomaly_time=stop_learning_no_anomaly_time
        )
        # This timestamp is compared with timestamp values from log atoms for activation of alerting logic. The first timestamp from logs
        # above this value will trigger alerting.
        self.next_check_timestamp = 0
        self.last_seen_timestamp = 0
        self.log_learned_values = 0
        self.log_new_learned_values = []

        self.persistence_file_name = build_persistence_file_name(aminer_config, self.__class__.__name__, persistence_id)
        PersistenceUtil.add_persistable_component(self)
        persistence_data = PersistenceUtil.load_json(self.persistence_file_name)
        self.expected_values_dict = {}
        if persistence_data is not None:
            for key in persistence_data:
                value = persistence_data[key]
                if self.target_path_list is not None:  # skipcq: PTC-W0048
                    if (value[3] not in self.target_path_list and not self.combine_values) or (
                            value[3] != str(self.target_path_list) and self.combine_values):
                        continue
                elif self.target_path_list is not None and value[3] not in self.target_path_list:
                    continue
                if value[1] != default_interval:
                    value[1] = default_interval
                    value[2] = value[0] + default_interval
                self.expected_values_dict[key] = value
            logging.getLogger(DEBUG_LOG_NAME).debug(f'{self.__class__.__name__} loaded persistence data.')
        self.analysis_string = 'Analysis.%s'

    def receive_atom(self, log_atom):
        """
        Receive a log atom from a source.
        @param log_atom binary raw atom data
        @return True if this handler was really able to handle and process the atom. Depending on this information, the caller
                may decide if it makes sense passing the atom also to other handlers or to retry later. This behaviour has to be documented
                at each source implementation sending log atoms.
        """
        self.log_total += 1
        if self.learn_mode is True and self.stop_learning_timestamp is not None and \
                self.stop_learning_timestamp < log_atom.atom_time:
            logging.getLogger(DEBUG_LOG_NAME).info(f"Stopping learning in the {self.__class__.__name__}.")
            self.learn_mode = False

        value = self.get_channel_key(log_atom)
        if value is None or (not value[0] and not value[1]):
            return False
        target_paths, value_list = value
        if isinstance(target_paths, str) and isinstance(value_list, str):
            target_paths = [target_paths]
            value_list = [value_list]
        timestamp = log_atom.get_timestamp()
        if timestamp is None:
            timestamp = time.time()
        for i, target_path in enumerate(target_paths):
            value = value_list[i]
            detector_info = self.expected_values_dict.get(value)
            if detector_info is None and self.learn_mode:
                self.expected_values_dict[value] = [timestamp, self.default_interval, 0, target_path]
                self.next_check_timestamp = min(self.next_check_timestamp, timestamp + self.default_interval)
                self.log_learned_values += 1
                self.log_new_learned_values.append(value)
                if self.stop_learning_timestamp is not None and self.stop_learning_no_anomaly_time is not None:
                    self.stop_learning_timestamp = time.time() + self.stop_learning_no_anomaly_time

        self.check_timeouts(timestamp, log_atom)

        for i, target_path in enumerate(target_paths):
            value = value_list[i]
            detector_info = self.expected_values_dict.get(value)
            if detector_info is not None:
                # Just update the last seen value and switch from non-reporting error state to normal state.
                detector_info[0] = timestamp
                if detector_info[2] != 0:
                    if timestamp >= detector_info[2]:
                        detector_info[2] = 0
                    # Delta of this detector might be lower than the default maximum recheck time.
                    self.next_check_timestamp = min(self.next_check_timestamp, timestamp + detector_info[1])
        self.log_success += 1
        return True

    def get_channel_key(self, log_atom):
        """Get the key identifying the channel this log_atom is coming from."""
        value_list = []
        path_list = []
        for target_path in self.target_path_list:
            match = log_atom.parser_match.get_match_dictionary().get(target_path)
            if match is None:
                if self.combine_values:
                    return None
                continue
            matches = []
            if isinstance(match, list):
                matches = match
            else:
                matches.append(match)
            for match in matches:
                if isinstance(match.match_object, bytes):
                    affected_log_atom_values = match.match_object.decode(AminerConfig.ENCODING)
                else:
                    affected_log_atom_values = match.match_object
                value_list.append(str(affected_log_atom_values))
                path_list.append(target_path)
        if self.combine_values:
            value_list = str(value_list)
            path_list = str(path_list)
        return path_list, value_list

    def check_timeouts(self, timestamp, log_atom):
        """Check if there was any timeout on a channel, thus triggering event dispatching."""
        old_last_seen_timestamp = self.last_seen_timestamp
        self.last_seen_timestamp = max(self.last_seen_timestamp, timestamp)
        if self.last_seen_timestamp > self.next_check_timestamp:
            missing_value_list = []
            # Start with a large recheck interval. It will be lowered if any of the expectation intervals is below that.
            if self.next_check_timestamp == 0:
                self.next_check_timestamp = self.last_seen_timestamp + self.realert_interval
            for value, detector_info in self.expected_values_dict.items():
                value_overdue_time = int(self.last_seen_timestamp - detector_info[0] - detector_info[1])
                if detector_info[2] != 0:
                    next_check_delta = detector_info[2] - self.last_seen_timestamp
                    if next_check_delta > 0:
                        # Already alerted but not ready for realerting yet.
                        self.next_check_timestamp = min(self.next_check_timestamp, detector_info[2])
                        continue
                else:
                    # No alerting yet, see if alerting is required.
                    if value_overdue_time < 0:
                        old = self.next_check_timestamp
                        self.next_check_timestamp = min(self.next_check_timestamp, self.last_seen_timestamp - value_overdue_time)
                        if old > self.next_check_timestamp or self.next_check_timestamp < detector_info[2]:
                            break
                # avoid early re-alerting
                if value_overdue_time > 0:
                    missing_value_list.append([detector_info[3], value, value_overdue_time, detector_info[1]])
                    # Set the next alerting time.
                    detector_info[2] = self.last_seen_timestamp + self.realert_interval
                    self.expected_values_dict[value] = detector_info
                # Workaround:
                # also check for long gaps between same tokens where the last_seen_timestamp gets updated
                # on the arrival of tokens following a longer gap
                elif self.last_seen_timestamp - detector_info[0] > detector_info[1]:
                    value_overdue_time = self.last_seen_timestamp - old_last_seen_timestamp - detector_info[1]
                    missing_value_list.append([detector_info[3], value, value_overdue_time, detector_info[1]])
                    # Set the next alerting time.
                    detector_info[2] = self.last_seen_timestamp + self.realert_interval
                    self.expected_values_dict[value] = detector_info
            if missing_value_list:
                message_part = []
                affected_log_atom_values = []
                for target_path_list, value, overdue_time, interval in missing_value_list:
                    e = {}
                    try:
                        if isinstance(value, list):
                            data = []
                            for val in value:
                                if isinstance(val, bytes):
                                    data.append(val.decode(AminerConfig.ENCODING))
                                else:
                                    data.append(val)
                            data = str(data)
                        else:
                            if isinstance(value, bytes):
                                data = value.decode(AminerConfig.ENCODING)
                            else:
                                data = repr(value)
                    except UnicodeError:
                        data = repr(value)
                    if self.__class__.__name__ == 'MissingMatchPathValueDetector':
                        e['TargetPathList'] = target_path_list
                        message_part.append(f'  {target_path_list}: {data} overdue {overdue_time}s (interval {interval})\n')
                    else:
                        target_paths = ''
                        for target_path in self.target_path_list:
                            target_paths += target_path + ', '
                        e['TargetPathList'] = self.target_path_list
                        message_part.append(f'  {target_paths[:-2]}: {data} overdue {overdue_time}s (interval {interval})\n')
                    e['Value'] = str(value)
                    e['OverdueTime'] = str(overdue_time)
                    e['Interval'] = str(interval)
                    affected_log_atom_values.append(e)
                affected_log_atom_paths = []
                for path in log_atom.parser_match.get_match_dictionary().keys():
                    if path in self.target_path_list:
                        affected_log_atom_paths.append(path)
                analysis_component = {'AffectedLogAtomPaths': affected_log_atom_paths,
                                      'AffectedLogAtomValues': affected_log_atom_values}
                event_data = {'AnalysisComponent': analysis_component}
                for listener in self.anomaly_event_handlers:
                    self.send_event_to_handlers(listener, event_data, log_atom, [''.join(message_part).strip()])
        return True

    def send_event_to_handlers(self, anomaly_event_handler, event_data, log_atom, message_part):
        """Send an event to the event handlers."""
        anomaly_event_handler.receive_event(self.analysis_string % self.__class__.__name__, 'Interval too large between values',
                                            message_part, event_data, log_atom, self)

    def set_check_value(self, value, interval, target_path):
        """Add or overwrite a value to be monitored by the detector."""
        self.expected_values_dict[value] = [self.last_seen_timestamp, interval, 0, target_path]
        self.next_check_timestamp = 0

    def remove_check_value(self, value):
        """Remove checks for given value."""
        del self.expected_values_dict[value]
        logging.getLogger(DEBUG_LOG_NAME).debug(f'{self.__class__.__name__} removed check value {str(value)}.')

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
        PersistenceUtil.store_json(self.persistence_file_name, self.expected_values_dict)
        logging.getLogger(DEBUG_LOG_NAME).debug(f'{self.__class__.__name__} persisted data.')

    def allowlist_event(self, event_type, event_data, allowlisting_data):
        """
        Allowlist an event generated by this source using the information emitted when generating the event.
        @return a message with information about allowlisting using given allowlisting_data was not possible.
        """
        if event_type != self.analysis_string % self.__class__.__name__:
            msg = 'Event not from this source'
            logging.getLogger(DEBUG_LOG_NAME).error(msg)
            raise Exception(msg)
        if not isinstance(allowlisting_data, int):
            msg = 'Allowlisting data has to integer with new interval, -1 to reset to defaults, other negative value to remove the entry'
            logging.getLogger(DEBUG_LOG_NAME).error(msg)
            raise Exception(msg)
        new_interval = allowlisting_data
        if new_interval == -1:
            new_interval = self.default_interval
        if new_interval < 0:
            self.remove_check_value(event_data[0])
        else:
            self.set_check_value(event_data[0], new_interval, event_data[1])
        return f"Updated '{event_data[0]}' in '{event_data[1]}' to new interval {new_interval}."

    def log_statistics(self, component_name):
        """
        Log statistics of an AtomHandler. Override this method for more sophisticated statistics output of the AtomHandler.
        @param component_name the name of the component which is printed in the log line.
        """
        if AminerConfig.STAT_LEVEL == 1:
            logging.getLogger(STAT_LOG_NAME).info(
                f"'{component_name}' processed {self.log_success} out of {self.log_total} log atoms successfully and learned "
                f"{self.log_learned_values} new values in the last 60 minutes.)")
        elif AminerConfig.STAT_LEVEL == 2:
            logging.getLogger(STAT_LOG_NAME).info(
                f"'{component_name}' processed {self.log_success} out of {self.log_total} log atoms successfully and learned "
                f"{self.log_learned_values} new values in the last 60 minutes. Following new values were learned: "
                f"{self.log_new_learned_values}")
        self.log_success = 0
        self.log_total = 0
        self.log_learned_values = 0
        self.log_new_learned_values = []


class MissingMatchPathListValueDetector(MissingMatchPathValueDetector):
    """
    This detector works similar to the MissingMatchPathValueDetector.
    It only can lookup values from a list of paths until one path really exists. It then uses this value as key to detect logAtoms
    belonging to the same data stream. This is useful when e.g. due to different log formats, the hostname, servicename or any other
    relevant channel identifier has alternative paths.
    """

    def get_channel_key(self, log_atom):
        """Get the key identifying the channel this log_atom is coming from."""
        for target_path in self.target_path_list:
            match_element = log_atom.parser_match.get_match_dictionary().get(target_path)
            if match_element is None:
                continue
            if isinstance(match_element.match_object, bytes):
                affected_log_atom_values = match_element.match_object.decode(AminerConfig.ENCODING)
            else:
                affected_log_atom_values = match_element.match_object
            return target_path, str(affected_log_atom_values)
        return None

    def send_event_to_handlers(self, anomaly_event_handler, event_data, log_atom, message_part):
        """Send an event to the event handlers."""
        anomaly_event_handler.receive_event(self.analysis_string % self.__class__.__name__, 'Interval too large between values',
                                            message_part, event_data, log_atom, self)

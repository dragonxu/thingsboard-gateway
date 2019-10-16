import copy
import logging
import io
import os
import base64
import json
from json.decoder import JSONDecodeError
from storage.event_storage_files import EventStorageFiles
from storage.file_event_storage_settings import FileEventStorageSettings
from storage.event_storage_reader_pointer import EventStorageReaderPointer

log = logging.getLogger(__name__)


class EventStorageReader:
    def __init__(self, files: EventStorageFiles, settings: FileEventStorageSettings):
        self.files = files
        self.settings = settings
        self.current_batch = None
        self.buffered_reader: io.BufferedReader = None
        self.current_pos = self.read_state_file()
        self.new_pos = copy.deepcopy(self.current_pos)

    def read(self):
        log.debug("[{}:{}] Check for new messages in storage".format(self.new_pos.get_file(), self.new_pos.get_line()))
        if self.current_batch is not None and self.current_pos != self.new_pos:
            log.debug("The previous batch was not discarded!")
            return self.current_batch
        self.current_batch = []
        records_to_read = self.settings.get_max_read_records_count()
        while records_to_read > 0:
            try:
                current_line_in_file = self.new_pos.get_line()
                reader = self.get_or_init_buffered_reader(self.new_pos)
                line = reader.readline()
                # TODO Better way to assign line
                while line is not None:
                    line = reader.readline()
                    try:
                        self.current_batch.append(base64.b64decode(line).decode("utf-8"))
                        records_to_read -= 1
                    except IOError as e:
                        log.warning("Could not parse line [{}] to uplink message!".format(line), e)
                    finally:
                        current_line_in_file += 1
                    self.new_pos.set_line(current_line_in_file)
                    if records_to_read == 0:
                        break

                if current_line_in_file == self.settings.get_max_records_per_file():
                    next_file = self.get_next_file(self.files, self.new_pos)
                    if next_file is not None:
                        if self.buffered_reader is not None:
                            self.buffered_reader.close()
                        self.buffered_reader = None
                        self.new_pos = EventStorageReaderPointer(next_file, 0)
                    else:
                        # No more records to read for now
                        break
                else:
                    # No more records to read for now
                    break
            except IOError as e:
                log.warning("[{}] Failed to read file!".format(self.new_pos.get_file(), e))
                break
        log.debug("Got {} mesages from storage".format(len(self.current_batch)))
        return self.current_batch

    def discard_batch(self):
        self.current_pos = copy.deepcopy(self.new_pos)
        self.write_info_to_state_file(self.current_pos)

    def get_next_file(self, files: EventStorageFiles, new_pos: EventStorageReaderPointer):
        found = False
        for file in files.get_data_files():
            if found:
                return file
            if file == new_pos.get_file():
                found = True
        if found:
            return None
        else:
            return files.get_data_files()[0]

    def get_or_init_buffered_reader(self, pointer):
        try:
            if self.buffered_reader is None:
                self.buffered_reader = io.BufferedReader(io.FileIO(
                    self.settings.get_data_folder_path() + pointer.get_file(), 'r'))
            lines_to_skip = pointer.get_line()
            if lines_to_skip > 0:
                while self.buffered_reader.readline() is not None:
                    if lines_to_skip != 0:
                        lines_to_skip -= 1
                    else:
                        break
            return self.buffered_reader

        except IOError as e:
            log.error("Failed to initialize buffered reader!", e)
            raise RuntimeError("Failed to initialize buffered reader!", e)

    def read_state_file(self):
        state_data_node = {}
        try:
            with io.BufferedReader(io.FileIO(self.settings.get_data_folder_path() +
                                             self.files.get_state_file(), 'r')) as br:
                state_data_node = json.load(br)
        except JSONDecodeError:
            log.error("Failed to decode JSON from state file")
            state_data_node = 0
        except IOError as e:
            log.warning("Failed to fetch info from state file!", e)
        reader_file = None
        reader_pos = 0
        if state_data_node:
            reader_pos = state_data_node['position']
            for file in sorted(self.files.get_data_files()):
                if file == state_data_node['file']:
                    reader_file = file
                    break
        if reader_file is None:
            reader_file = sorted(self.files.get_data_files())[0]
            reader_pos = 0
        log.info("Initializing from state file: [{}:{}]".format(
            self.settings.get_data_folder_path() + reader_file, reader_pos))
        return EventStorageReaderPointer(reader_file, reader_pos)

    def write_info_to_state_file(self, pointer: EventStorageReaderPointer):
        try:
            state_file_node = {'file': pointer.get_file(), 'position': pointer.get_line()}
            with open(self.files.get_state_file(), 'w') as outfile:
                json.dump(state_file_node, outfile)
        except IOError as e:
            log.warning("Failed to update state file!", e)

    def destroy(self):
        if self.buffered_reader is not None:
            self.buffered_reader.close()
            raise IOError

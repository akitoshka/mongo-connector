"""Tails the oplog of a shard and returns entries
"""

import os
import time
import json
import pymongo

from pymongo import Connection
from bson.objectid import ObjectId
from bson.timestamp import Timestamp
from threading import Thread, Timer
from checkpoint import Checkpoint
from solr_doc_manager import SolrDocManager
from util import (bson_ts_to_long,
                  long_to_bson_ts,
		          retry_until_ok)



class OplogThread(Thread):
    """OplogThread gathers the updates for a single oplog. 
    """
    
    def __init__(self, primary_conn, mongos_address, oplog_coll, is_sharded,
     doc_manager, oplog_file, namespace_set):
        """Initialize the oplog thread.
        """
        super(OplogThread, self).__init__()
        self.primary_connection = primary_conn
        self.mongos_address = mongos_address
        self.oplog = oplog_coll
        self.is_sharded = is_sharded
        self.doc_manager = doc_manager
        self.running = False
        self.checkpoint = None
        self.oplog_file = oplog_file
        self.namespace_set = namespace_set 
        self.mongos_connection = Connection(mongos_address)
        
    def run(self):
        """Start the oplog worker.
        """
        self.running = True  
        
        if self.is_sharded is False:
            #print 'handle later'
            return
              
        while self.running is True:    
            
            #print 'in oplog thread for connection'
            #print self.primary_connection   
            cursor = self.prepare_for_sync()
            last_ts = None
            
            #print 'cursor count is ' + str(cursor.count())
            try:
                for entry in cursor:  
                    #print 'cursor entry is'
                    #print entry
                    operation = entry['op']
    
                    if operation == 'd':
                      #  doc_id = entry['o']['_id']
                        entry['_id'] = entry['o']['_id']
                        self.doc_manager.remove(entry)
                    
                    elif operation == 'i' or operation == 'u':
                        doc = self.retrieve_doc(entry)
                        #print 'in insert area'
                        
                        if doc is not None:
                            doc['_ts'] = bson_ts_to_long(entry['ts'])
                            doc['ns'] = entry['ns']
                            print 'in main run method, inserting doc'
                            print doc
                            self.doc_manager.upsert(doc) 
                            
                            
                    #sometimes you see the document, but don't follow
                    #through and insert, and the timestamp gets written
                    #anyways
                last_ts = entry['ts']
            except:
                time.sleep(2)
                continue
                
            if last_ts is not None:                 #we actually processed docs
                self.checkpoint.commit_ts = last_ts
                self.write_config()
                
            time.sleep(2)   #for testing purposes
            
    
    
    def stop(self):
        """Stop this thread from managing the oplog.
        """
        self.running = False
            
            
    def retrieve_doc(self, entry):
        """Given the doc ID's, retrieve those documents from the mongos.
        """
        if (entry is None):
            return None

        namespace = entry['ns']
        if entry.has_key('o2'):
            doc_field = 'o2'
        else:
            doc_field = 'o'
        
        doc_id = entry[doc_field]['_id']
        #print 'in retrieve doc'
        #print doc_id 
        db_name, coll_name = namespace.split('.',1)

        while True:
            try :
                coll = self.mongos_connection[db_name][coll_name]
                doc = coll.find_one({'_id': doc_id})
                #print 'found doc'
                #print doc
                break
            except :
                time.sleep(1)
                continue

        return doc
    
    
    def get_oplog_cursor(self, timestamp):
        """Move cursor to the proper place in the oplog. 
        """
        ret = None
        
        if timestamp is None:
            return None
              
        cursor = self.oplog.find({'ts': {'$gte': timestamp}}, tailable=True,
        await_data=True).sort('$natural', pymongo.ASCENDING) 
    
        try: 
            # we should re-read the last committed document
            doc = cursor.next() 
            #print doc
            if timestamp == doc['ts']: 
                '#print returning up to date cursor'
                time.sleep(1)  
                ret = cursor 
            else:
                return None
        except:
            entry = retry_until_ok(self.oplog.find_one, '{\'ts\':timestamp}')
            if entry is None:
                less_than_doc = self.oplog.find_one({'ts': {'$lt':timestamp}})
                if less_than_doc:
                    ret = self.get_oplog_cursor(self.rollback())
            else:
                ret = cursor
        
        return ret
        
    def get_last_oplog_timestamp(self):
        """Return the timestamp of the latest entry in the oplog.
        """
        curr = self.oplog.find().sort('$natural',pymongo.DESCENDING).limit(1)
        if curr.count(with_limit_and_skip= True) == 0 :
            return None
        return curr[0]['ts']
        
    #used here for testing, eventually we will use last_oplog_ts() + full_dump()
    def get_first_oplog_timestamp(self):
        """Return the timestamp of the first entry in the oplog.
        """
        curr = self.oplog.find().sort('$natural',pymongo.ASCENDING).limit(1)
        return curr[0]['ts']
        
    
    def dump_collection(self, timestamp):
        """Dumps collection into backend engine.
        
        This method is called when we're initializing the cursor and have no
        configs i.e. when we're starting for the first time.
        """
        if timestamp == None:
            return None
        for namespace in self.namespace_set:
            db, coll = namespace.split('.', 1)
            cursor = self.primary_connection[db][coll].find()
            long_ts = bson_ts_to_long(timestamp)

            for doc in cursor:
                #print 'in dump collection'
                #print doc
                doc['ns'] = namespace
                doc['_ts'] = long_ts
                self.doc_manager.upsert(doc)
            
    
    def init_cursor(self):
        """Position the cursor appropriately.
        
        The cursor is set to either the beginning of the oplog, or wherever it was 
        last left off. 
        """
        timestamp = self.read_config()
        
        if timestamp is None:
            timestamp = self.get_last_oplog_timestamp()
            self.dump_collection(timestamp)
            
        self.checkpoint.commit_ts = timestamp
        self.write_config()
        cursor = self.get_oplog_cursor(timestamp)
        
        return cursor
            
        
    def prepare_for_sync(self):
        """ Initializes the cursor for the sync method. 
        """
        cursor = None
        last_commit = None

        if self.checkpoint is None:
            self.checkpoint = Checkpoint()
            cursor = self.init_cursor()
        else:
            last_commit = self.checkpoint.commit_ts
            #print 'getting oplog cursor'
            cursor = self.get_oplog_cursor(last_commit)
            
            if cursor is None:
                cursor = self.init_cursor()
                    
        return cursor
        
        
    def write_config(self):
        """
        Write the updated config to the config file. 
        
        This is done by duplicating the old config file, editing the relevant
        timestamp, and then copying the new config onto the old file. 
        """
        if self.oplog_file is None:
            return None

        os.rename(self.oplog_file, self.oplog_file + '~')  # temp file
        dest = open(self.oplog_file, 'w')
        source = open(self.oplog_file + '~', 'r')
        oplog_str = str(self.oplog.database.connection)
        
        timestamp = bson_ts_to_long(self.checkpoint.commit_ts)
        json_str = json.dumps([oplog_str, timestamp])
        dest.write(json_str) 
            
        for line in source:
            if oplog_str in line:
                continue                        # we've already updated
            else:
                dest.write(line)
  
        
        source.close()
        dest.close()
        os.remove(self.oplog_file+'~')
        

    def read_config(self):
        """Read the config file for the relevant timestamp, if possible.
        """      
        config_file = self.oplog_file
        if config_file is None:
            #print 'Need a config file!'
            return None
        
        source = open(self.oplog_file, 'r')
        try: 
            data = json.load(source)
        except:                                             # empty file
            return None
        
        oplog_str = str(self.oplog.database.connection)
        
        count = 0
        while (count < len(data)):
            if oplog_str in data[count]:                    #next line has time
                count = count + 1
                self.checkpoint.commit_ts = long_to_bson_ts(data[count])
                break
            count = count + 2                               # skip to next set
            
        return self.checkpoint.commit_ts
        
    
    def rollback(self):
        """Rollback backend engine to consistent state. 
        
        The strategy is to find the latest timestamp in the backend and 
        the largest timestamp in the oplog less than the latest backend
        timestamp. This defines the rollback window and we just roll these
        back until the oplog and backend are in consistent states. 
        """
        self.doc_manager.commit()
        last_inserted_doc = self.doc_manager.get_last_doc()
        
        if last_inserted_doc is None:
            return None

        backend_ts = long_to_bson_ts(last_inserted_doc['_ts'])
        last_oplog_entry = self.oplog.find_one({ 'ts': { '$lt':backend_ts} }, 
        sort= [('$natural',pymongo.DESCENDING)])
        
        if last_oplog_entry is None:
            return None
            
        rollback_cutoff_ts = last_oplog_entry['ts']
        start_ts = bson_ts_to_long(rollback_cutoff_ts)
        end_ts = last_inserted_doc['_ts']    
        
        docs_to_rollback = self.doc_manager.search(start_ts, end_ts)   
        
        rollback_set = {}
        for doc in docs_to_rollback:
            ns = doc['ns']
            
            if rollback_set.has_key(ns):
                rollback_set[ns].append(doc)
            else:
                rollback_set[ns] = [doc]
                
        for namespace, doc_list in rollback_set.items():
            db, coll = namespace.split('.', 1)
            bson_obj_id_list = [ObjectId(doc['_id']) for doc in doc_list]
            
            while True:
                try:
                    to_update = self.mongos_connection[db][coll].find({'_id': 
                        {'$in': bson_obj_id_list}})
                    break
                except:
                    pass
                    
            doc_hash = {}
            for doc in doc_list:
                doc_hash[ObjectId(doc['_id'])] = doc
                
            to_index = []

            try:
                for doc in to_update:
                    del doc_hash[doc['_id']]
                    to_index.append(doc)
            except:
                pass
                    
            print doc_hash
            #delete the inconsistent documents
            try:
                for doc in doc_hash.values():
                    self.doc_manager.remove(doc)
            except:
                pass
                        
            for doc in to_index:
                doc['_ts'] = bson_ts_to_long(rollback_cutoff_ts)
                doc['ns'] = namespace
                self.doc_manager.upsert(doc)
         
        return rollback_cutoff_ts
                       
                
            
            
            
            
            
            
            
            
            
            
            
            
        
        
        
        
        
    

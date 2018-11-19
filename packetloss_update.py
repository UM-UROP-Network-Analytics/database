#!/usr/bin/python
import elasticsearch
from elasticsearch import helpers
from datetime import datetime, timedelta
import time
import numpy as np
import psycopg2
from psycopg2 import IntegrityError
from config import config
import os
import os.path 
from pathlib import Path

#checks to see if this process is currently running
#if not running, make a lock file
lock_file = Path("/var/lock/plDBupdate")
if lock_file.is_file():
  print'Detected instance of process already running at ' + str(datetime.now())
  quit()
else:
  print 'Starting to create lock at ' + str(datetime.now())
  lk_file = open('/var/lock/plDBupdate', "w")
  print 'Lock created at ' + str(datetime.now()) 
  lk_file.close()
  print 'File closed at ' + str(datetime.now())

#connect to the database
es = elasticsearch.Elasticsearch(['atlas-kibana.mwt2.org:9200'],timeout=60)
my_index = ["ps_packetloss-2018*"]
params = config()
conn = psycopg2.connect(**params)
cur = conn.cursor()
my_query = {}
print 'Database connection established at ' + str(datetime.now())

#determine start and end times
now = datetime.utcnow()
curr_mon = now.month
curr_day = now.strftime("%d")
curr_year = now.strftime("%Y")
curr_hr = now.strftime("%H")
curr_min = now.strftime("%M")
curr_sec = now.strftime("%S")
end_date = curr_year + now.strftime("%m") + curr_day + 'T' + curr_hr + curr_min + curr_sec + 'Z'
cur.execute("SELECT * FROM rawpacketdata limit 1")
#if None, raw table empty, start from beginning of year
if cur.fetchone() is None:
  start_date = '20180101T000000Z'
#else start from 1 second past latest entry in raw table
else:
  cur.execute("SELECT to_char(max(timestamp+interval '1 sec'),'YYYYMMDD\"T\"HHMISS\"Z\"') FROM rawpacketdata")
  start_date = cur.fetchone()[0]
print 'Dates set at ' + str(datetime.now())

#build and run the query
my_query = {
    "size":1,
    "_source": {
        "include": [ 'src','dest', 'packet_loss', 'timestamp', 'src_host', 'dest_host', 'src_site', 'dest_site']
    },
    'query':{
        'bool':{
            'must':[
                {'range': {'timestamp': {'gte': start_date, 'lt': end_date}}},
            ]

        }
    },
}
results = elasticsearch.helpers.scan(es, query=my_query, index=my_index, raise_on_error = True, request_timeout=100000, size=1000)
print 'Results compiled at ' + str(datetime.now())

#updates the raw traceroute data table
def updateRaw( item ):
    rt_src = item['_source']['src']
    rt_dest = item['_source']['dest']
    rt_loss = item['_source']['packet_loss']
    rt_ts = item['_source']['timestamp']
    rt_ts = rt_ts / 1000
    #match appropriate format for the database timestamps
    format_ts = time.strftime("%Y-%m-%d %H:%M:%S-0000", time.gmtime(rt_ts))
    try:
        #try to insert the new data into the raw data table
        cur.execute("INSERT INTO rawpacketdata (src, dest, loss, timestamp) VALUES (%s, %s, %s, %s)", (rt_src, rt_dest, rt_loss, format_ts))
        conn.commit()
    except IntegrityError:
        #if IntegrityError, failed due to key violation (src, dest, timestamp triple already in table)
        #in that case, continue (skip to next iteration of loop)
        conn.rollback()
        continue

#updates the server lookup table
def updateLookup ( item ):
    rt_src = item['_source']['src']
    rt_dest = item['_source']['dest']
    src_name = item['_source']['src_host']
    dest_name = item['_source']['dest_host']
    if 'src_site' in item['_source'].keys():
        src_site = item['_source']['src_site']
    else:
        src_site = 'missing'
    if 'dest_site' in item['_source'].keys():
        dest_site = item['_source']['dest_site']
    else:
        dest_site = 'missing'
    #source is ipv6
    if ':' in rt_src:
        #mark that this source is a latency node
        cur.execute("UPDATE serverlookup SET bandwidth = %s WHERE ipv6 = %s", ('0', rt_src))
        conn.commit()
        #check if we have this ipv6 stored already
        cur.execute("SELECT ipv6 FROM serverlookup WHERE ipv6 = (%s)", (rt_src,))
        if cur.fetchone() is None:
            #if not, check if we have an ipv4 under the same domain (dual-stacked domain)
            cur.execute("SELECT ipv4 FROM serverlookup WHERE domain = (%s)", (src_name,))
            if cur.fetchone() is None:
                #if not, insert as a new domain
                cur.execute("INSERT INTO serverlookup (domain, ipv6, sitename) VALUES (%s, %s, %s)", (src_name, rt_src, src_site))
                conn.commit()
            else:
                #if we do, then update that domain's line in table to include the ipv6 as well as the ipv4
                cur.execute("UPDATE serverlookup SET domain = %s, ipv6 = %s WHERE domain = %s", (src_name, rt_src, src_name))
                conn.commit()
                cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (src_name,))
                #if we didn't have a sitename yet and do now, add it!
                if cur == 'missing':
                    cur.execute("UPDATE serverlookup SET sitename = %s WHERE domain = %s", (src_site, src_name))
                    conn.commit()
        else:
            #if we do have that ipv6, make sure domain is up to date
            cur.execute("UPDATE serverlookup SET domain = %s, ipv6 = %s WHERE ipv6 = %s", (src_name, rt_src, rt_src))
            conn.commit()
            cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (src_name,))
            #fix sitename if possible
            if cur == 'missing':
                cur.execute("UPDATE serverlookup SET sitename = %s WHERE ipv6 = %s", (src_site, rt_src))
                conn.commit()
    #source is ipv4
    else:
        #indicate that the source is a latency node
        cur.execute("UPDATE serverlookup SET bandwidth = %s WHERE ipv4 = %s", ('0', rt_src))
        conn.commit()
        #check if we have a row associated with that ipv4 yet
        cur.execute("SELECT ipv4 FROM serverlookup WHERE ipv4 = (%s)", (rt_src,))
        if cur.fetchone() is None:
            #if not, check if we have an ipv6 under the same domain
            cur.execute("SELECT ipv6 FROM serverlookup WHERE domain = (%s)", (src_name,))
            if cur.fetchone() is None:
                #if not, insert as a new domain
                cur.execute("INSERT INTO serverlookup (domain, ipv4, sitename) VALUES (%s, %s, %s)", (src_name, rt_src, src_site))
                conn.commit()
            else:
                #if we have it, update that domain's line in lookup table to include the ipv4 as well as the ipv6
                cur.execute("UPDATE serverlookup SET domain = %s, ipv4 = %s WHERE domain = %s", (src_name, rt_src, src_name))
                conn.commit()
                cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (src_name,))
                #add sitename if it was missing before
                if cur == 'missing':
                    cur.execute("UPDATE serverlookup SET sitename = %s WHERE domain = %s", (src_site, src_name))
                    conn.commit()
        else:
            #if we do already have that ipv4, make sure domain is up to date
            cur.execute("UPDATE serverlookup SET domain = %s, ipv4 = %s WHERE ipv4 = %s", (src_name, rt_src, rt_src))
            conn.commit()
            cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (src_name,))
            #fix sitename if possible
            if cur == 'missing':
                cur.execute("UPDATE serverlookup SET sitename = %s WHERE ipv4 = %s", (src_site, rt_src))
                conn.commit()
    #for destination, logic at each individual step is same as the logic for the source nodes
    #destination is ipv6
    if ':' in rt_dest:      
        cur.execute("SELECT ipv6 FROM serverlookup WHERE ipv6 = (%s)", (rt_dest,))
        if cur.fetchone() is None:
            cur.execute("SELECT ipv4 FROM serverlookup WHERE domain = (%s)", (dest_name,))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO serverlookup (domain, ipv6, sitename) VALUES (%s, %s, %s)", (dest_name, rt_dest, dest_site))
                conn.commit()
            else:
                cur.execute("UPDATE serverlookup SET domain = %s, ipv6 = %s WHERE domain = %s", (dest_name, rt_dest, dest_name))
                conn.commit()
                cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (dest_name,))
                if cur == 'missing':
                    cur.execute("UPDATE serverlookup SET sitename = %s WHERE domain = %s", (dest_site, dest_name))
                    conn.commit()
        else:
            cur.execute("UPDATE serverlookup SET domain = %s, ipv6 = %s WHERE ipv6 = %s", (dest_name, rt_dest, rt_dest))
            conn.commit()
            cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (dest_name,))
            if cur == 'missing':
                cur.execute("UPDATE serverlookup SET sitename = %s WHERE ipv6 = %s", (dest_site, rt_dest))
                conn.commit()
    #destination is ipv4
    else:
        cur.execute("SELECT ipv4 FROM serverlookup WHERE ipv4 = (%s)", (rt_dest,))
        if cur.fetchone() is None:
            cur.execute("SELECT ipv6 FROM serverlookup WHERE domain = (%s)", (dest_name,))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO serverlookup (domain, ipv4, sitename) VALUES (%s, %s, %s)", (dest_name, rt_dest, dest_site))
                conn.commit()
            else:
                cur.execute("UPDATE serverlookup SET domain = %s, ipv4 = %s WHERE domain = %s", (dest_name, rt_dest, dest_name))
                conn.commit()
                cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (dest_name,))
                if cur == 'missing':
                    cur.execute("UPDATE serverlookup SET sitename = %s WHERE domain = %s", (dest_site, dest_name))
                    conn.commit()
        else:
            cur.execute("UPDATE serverlookup SET domain = %s, ipv4 = %s WHERE ipv4 = %s", (dest_name, rt_dest, rt_dest))
            conn.commit()
            cur.execute("SELECT sitename FROM serverlookup WHERE domain = %s", (dest_name,))
            if cur == 'missing':
                cur.execute("UPDATE serverlookup SET sitename = %s WHERE ipv4 = %s", (dest_site, rt_dest))
                conn.commit()

#updates the unique count table as well as the summary table
def updateSummary( item ):
    rt_src = item['_source']['src']
    rt_dest = item['_source']['dest']
    rt_ts = item['_source']['timestamp']
    rt_ts = rt_ts / 1000
    #fix timestamp formatting to match postgreSQL format
    format_ts = time.strftime("%Y-%m-%d %H:%M:%S-0000", time.gmtime(rt_ts))
    try:
        #if this src/dest pair not yet tracked in packet loss, add it w/ count of 1
        cur.execute("INSERT INTO losscount (src, dest, count, min_ts, max_ts) VALUES (%s, %s, %s)", (rt_src, rt_dest, 1, format_ts, format_ts))
        conn.commit()
    except IntegrityError:
        conn.rollback()
        #if it has been tracked, increment count by 1
        cur.execute("SELECT count FROM losscount WHERE src = %s AND dest = %s", (rt_src, rt_dest))
        current_count = cur.fetchone()[0]
        #make sure that we somehow aren't missing a min_ts for an existing route
        cur.execute("SELECT min_ts FROM losscount WHERE src = %s AND dest = %s", (rt_src, rt_dest))
        min_check = cur.fetchone()[0]
        if current_count is None:
            if min_check is None:
                #error case w/ missing min_ts
                cur.execute("UPDATE losscount SET count = %s, min_ts = %s, max_ts = %s WHERE src = %s AND dest = %s", (1, format_ts, format_ts, rt_src, rt_dest))
                conn.commit()
            else:
                #account for error case where src/dest pair in table but count in null for some reason
                cur.execute("UPDATE losscount SET count = %s, max_ts = %s WHERE src = %s AND dest = %s", (1, format_ts, rt_src, rt_dest))
                conn.commit()
        else:
            if min_check is None:
                #error case w/ missing min_ts
                cur.execute("UPDATE losscount SET count = %s, min_ts = %s, max_ts = %s WHERE src = %s AND dest = %s", (1, format_ts, format_ts, rt_src, rt_dest))
                conn.commit()
            else:
                #else add 1 to count normally
                cur.execute("UPDATE losscount SET count = %s, max_ts WHERE src = %s AND dest = %s", (current_count+1, format_ts, rt_src, rt_dest))
                conn.commit()

#remove lock
def rm_lock():
    print 'Starting to remove lock at ' + str(datetime.now())
    os.remove('/var/lock/plDBupdate')
    print 'Lock removed at ' + str(datetime.now())
    
#loops through everything in results and then calls all update functions on each item
print 'The main loop of this run started at ' + str(datetime.now())
for item in results:
    updateRaw(item)
    updateLookup(item)
    updateSummary(item)

import atexit
atexit.register(rm_lock)

print 'This run finished at ' + str(datetime.now())
cur.close()
conn.close()
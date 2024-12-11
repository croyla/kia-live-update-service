import asyncio
import concurrent.futures
import time
from datetime import datetime, timedelta, timezone

# import boto3
import os
from dotenv import load_dotenv
import requests
from queue import Queue
import json
from flask import Flask
import traceback


# Read only constants
with open('client_stops.json') as f:
    ALL_BUSES_STOPS = json.load(f)

stop_names = {key: [stop['name'] for stop in route['stops']] for key, route in ALL_BUSES_STOPS.items()}

log_prefix = '[MAIN]'

load_dotenv()
# client = boto3.client('dynamodb')
# dynamodb = boto3.resource('dynamodb')  # get aws information from env variable
api_url = os.environ.get('BMTC_API_URL', 'https://bmtcmobileapistaging.amnex.com/WebAPI')
end = 'END_LOOP_QUEUE'  # Terminator for threaded operations
# Every day: query timetable for all KIA routes for next day
# At the time of kia route start (and up to 25 mins prior / after at 5 min intervals):
# query BMTC API for specific route every 30 seconds
# If specific kia route is already being queried, no need to query duplicate
# Query the route every 30 seconds till we stop getting data
# ~Update received data to Amazon DB~ Serve fresh data for clients to interact with via flask (Perhaps shift to a
# different option should demand increase)
with open('routes_children_ids.json') as f:
    ALL_ROUTES_CHILDREN_IDS = json.load(f)

routes_children = ALL_ROUTES_CHILDREN_IDS  # All routes

with open('routes_ids.json') as f:
    ALL_ROUTES_IDS = json.load(f)

routes = ALL_ROUTES_IDS  # All routes
request_headers = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.5',
    'Content-Type': 'application/json',
    'lan': 'en',
    'deviceType': 'WEB',
    'Origin': 'https://bmtcwebportal.amnex.com',
    'Referer': 'https://bmtcwebportal.amnex.com/'
}

# Mutable variables
queue = Queue()  # Will have all updates received from source
update_timings = Queue()  # Will have update timings for the day
update_timings_state = Queue(1)  # False means update_timings is currently being updated, True means it has been updated
update_timings_state.put(False)
currently_waiting_on = Queue(1)  # Waiting on set, will always have only 1 object
currently_waiting_on.put(set())
state_queue = Queue(1)  # Will always have only 1 object
state_queue.put({'last_update': datetime.now().astimezone(), 'data': {}})
updating = Queue(1)
updating.put(set(routes.values()))
timings_all = Queue(1)
timings_all.put([])
print(f'{log_prefix} Starting off with {routes.values()}')


def updater(r):
    async def update_loop_call(q: Queue,
                               r):  # Fetch data from bmtc api and update data_snapshot, in a separate thread
        # Await request to bmtc api with parent route id r
        # upon receiving q.put data for route
        # If received empty data then remove from set
        log_prefix = '[UPDATE_LOOP_CALL]'
        print(f'{log_prefix} received value to query {r}')
        waiting_on = currently_waiting_on.get()
        waiting_on.add(r)
        currently_waiting_on.put(waiting_on)
        print(f'{log_prefix} added {r} to waiting_on, now querying')
        if r == end:
            q.put(end)
            return
        response = requests.post(
            url=f'{api_url}/SearchByRouteDetails_v4',
            data=json.dumps({
                'routeid': r,
                'servicetypeid': 0
            }),
            headers=request_headers).json()
        print(f'{log_prefix} response has resolved for {r}')
        waiting_on = currently_waiting_on.get()
        waiting_on.remove(r)
        currently_waiting_on.put(waiting_on)
        print(f'{log_prefix} removed {r} from waiting_on')
        if ('up' in response.keys()
                and 'down' in response.keys()
                and 'data' in response['up']
                and 'data' in response['down']
                and len(response['up']['data']) == 0
                and len(response['down']['data']) == 0
        ):
            updating_s = updating.get()
            updating_s.remove(r)
            updating.put(updating_s)
        q.put(response)
        print(f'{log_prefix} put response for {r} in queue')

    asyncio.run(update_loop_call(queue, r))


def main_runner():
    def format_and_consume(req: dict):
        try:
            log_prefix = '[FORMAT_AND_CONSUME]'
            vehicles = {}
            for key in req.keys():
                if not type(req[key]) is dict:
                    continue
                if 'data' in req[key].keys() and len(req[key]['data']) > 0:
                    data_list = req[key]['data']
                    suffix = 'UP' if key == 'up' else ('DOWN' if key == 'down' else '')
                    name = ''
                    for stop_data in data_list:
                        if name == '':
                            name = f'{stop_data["routeno"]} {suffix}'
                        fallback_name = stop_names[name][0] if name in stop_names.keys() else 'None'
                        current_stop = stop_data["stationname"]
                        current_stop_id = stop_data["stationid"]
                        if 'vehicleDetails' in stop_data.keys():
                            stop_covered_all = 1
                            for vehicle_info in stop_data['vehicleDetails']:
                                if not vehicle_info['vehicleid'] in vehicles.keys():
                                    vehicles[vehicle_info['vehicleid']] = {
                                        'regno': vehicle_info['vehiclenumber'],
                                        'lat': vehicle_info['centerlat'],
                                        'long': vehicle_info['centerlong'],
                                        'refresh': vehicle_info['lastrefreshon'],
                                        'currentStop': current_stop,
                                        'lastStop': stop_data['from'],
                                        'lastKnownStop': stop_data['from']
                                        if name in stop_names.keys()
                                           and stop_data['from'] in stop_names[name]
                                        else fallback_name,
                                        'stopCovered': vehicle_info['stopCoveredStatus'],
                                        'stopCoveredOriginal': vehicle_info['stopCoveredStatus'],
                                        'routeno': name,
                                        'direction': suffix,
                                        'currentStopLocationId': vehicle_info['currentlocationid']
                                    }
                                if vehicles[vehicle_info['vehicleid']]['stopCovered'] == 1 or \
                                        vehicles[vehicle_info['vehicleid']]['stopCoveredOriginal'] == 0:
                                    stop_covered_all = 0

                                    if (name in stop_names.keys() and
                                            vehicles[vehicle_info['vehicleid']]['currentStop'] in stop_names[name]):
                                        vehicles[vehicle_info['vehicleid']]['lastKnownStop'] = \
                                            vehicles[vehicle_info['vehicleid']]['currentStop'] \
                                                if name in stop_names.keys() \
                                                   and vehicles[vehicle_info['vehicleid']]['currentStop'] in stop_names[
                                                       name] \
                                                else vehicles[vehicle_info['vehicleid']]['lastKnownStop']

                                    vehicles[vehicle_info['vehicleid']]['lastStop'] = \
                                        vehicles[vehicle_info['vehicleid']][
                                            'currentStop']
                                    vehicles[vehicle_info['vehicleid']]['currentStop'] = current_stop
                                    vehicles[vehicle_info['vehicleid']]['stopCovered'] = vehicle_info[
                                        'stopCoveredStatus']

                                    if vehicles[vehicle_info['vehicleid']]['currentStopLocationId'] == current_stop_id:
                                        vehicles[vehicle_info['vehicleid']]['stopCoveredOriginal'] = 1

                                if stop_covered_all == 1:
                                    break
                    old_full_data = state_queue.get().copy() if not state_queue.empty() else \
                        {'last_update': datetime.now().astimezone(), 'data': {}}
                    state_queue.put(old_full_data)
                    old_data = old_full_data['data']
                    old_data[name] = {'pollDate': datetime.now().astimezone().isoformat()}
                    for vehicle in vehicles.values():
                        last_known_stop = vehicle['lastKnownStop']

                        if last_known_stop not in old_data[name]:
                            old_data[name][last_known_stop] = []

                        old_data[name][last_known_stop].append(vehicle)
                    old_full_data['data'] = old_data
                    if not state_queue.empty():
                        state_queue.get()
                    state_queue.put(old_full_data)
                    print(f'{log_prefix} parsed data for {name}')
        except Exception as e:
            print(f'Received error {e}')
            print(traceback.format_exc())

    async def update_loop(q: Queue):  # Update data_snapshot very frequently, call update_loop_call
        try:
            log_prefix = '[MAIN_UPDATE_LOOP]'
            # Main loop
            data_snapshot = state_queue.get().copy() if not state_queue.empty() else {'last_update': datetime.now().astimezone(),
                                                                                      'data': {}}
            state_queue.put(data_snapshot)
            limit = datetime.now()
            next_update = update_timings.get().copy() if not update_timings.empty() else {}  # {'time': datetime.now(), 'key': 1}
            while data_snapshot['last_update'] < (datetime.now().astimezone() + timedelta(minutes=20)):
                #
                # Consume entire queue
                if not q.empty():
                    print(f'{log_prefix} Consuming all elements in queue')
                    while not q.empty():
                        try:
                            val = q.get()
                            if val is end:
                                print(f'{log_prefix} Received end command via queue')
                                if not state_queue.empty():
                                    state_queue.get()
                                state_queue.put(end)
                                return
                            format_and_consume(val)
                        except Exception as e:
                            print(f'Received error {e}')
                #
                # Run query loop only once every 30 seconds, don't want to overload bmtc servers
                if limit > datetime.now():
                    print(f'{log_prefix} Have additional time before limit to query API is exhausted, sleeping.')
                    time.sleep((limit - datetime.now()).total_seconds())
                    print(f'{log_prefix} Now awake to continue querying')
                limit = datetime.now() + timedelta(seconds=30)
                print(f'{log_prefix} Set query api limit for 30 seconds after now')
                #
                # Add next_update to updating list
                update_state = update_timings_state.get()
                update_timings_state.put(update_state)
                if update_state and 'time' in next_update.keys() and next_update['time'] < datetime.now().astimezone():
                    print(f'{log_prefix} Adding {next_update["key"]} to updating_set')
                    updating_set = updating.get()
                    updating_set.add(next_update['key'])
                    updating.put(updating_set)
                    next_update = update_timings.get() if not update_timings.empty() else {}
                #
                # Query API with latest live data
                with concurrent.futures.ThreadPoolExecutor() as update_executor:
                    waiting_on = currently_waiting_on.get()
                    currently_waiting_on.put(waiting_on)
                    print(f'{log_prefix} Currently waiting on ', waiting_on)
                    updating_set = updating.get()
                    # Due to pointers this is required, as we modify the set elsewhere
                    updating.put(updating_set.copy())
                    for r in updating_set:
                        if r not in waiting_on:
                            print(f'{log_prefix} Not waiting on {r}, querying')
                            try:
                                update_executor.submit(updater, r)
                            except Exception as e:
                                print(f'{log_prefix} encountered error while trying to run update_loop_call')
                                print(e)
                            finally:
                                time.sleep(1)  # sleep for a second after calling update_loop_call

                print(f'{log_prefix} Completed a round of calls')
                data_snapshot = state_queue.get()  # Update our data snapshot
                state_queue.put(data_snapshot)
                if next_update == {}:
                    next_update = update_timings.get() if not update_timings.empty() else {}
                elif next_update['time'] < datetime.now().astimezone():
                    updating_set = updating.get()
                    updating_set.add(next_update['key'])
                    updating.put(updating_set)
        except Exception as e:
            print(f'MAIN_RUNNER EXECUTION ERROR {e}')
            print(traceback.format_exc())

    # Run main update loop
    asyncio.run(update_loop(queue))


def writer():
    async def write_timings():  # Write timings to local queue every start of day
        log_prefix = '[WRITE_TIMINGS_LOOP]'
        timings = []
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        tomorrow_start = f'{tomorrow_str} 00:01'
        tomorrow_end = f'{tomorrow_str} 23:59'
        print(f'{log_prefix} Gathering timings for tomorrow from {tomorrow_start} to {tomorrow_end}')
        for key, route in routes_children.items():
            print(f'{log_prefix} Querying {route} routeid')
            response = requests.post(f'{api_url}/GetTimetableByRouteid_v3', headers=request_headers,
                                     data=json.dumps({
                                         'routeid': route,
                                         'starttime': tomorrow_start,
                                         'endtime': tomorrow_end,
                                         'current_date': tomorrow_str
                                     })).json()
            print(f'{log_prefix} Request for {route} routeid {key} key resolved')
            # print(f'{log_prefix} Response {response}')
            if 'data' in response.keys() and len(response['data']) > 0:
                print(f'{log_prefix} Parsing data for {key}')
                for response_data in response['data']:
                    print(f'{log_prefix} Currently in data entry for {key}')
                    if 'tripdetails' in response_data.keys() and len(response_data['tripdetails']) > 0:
                        print(f'{log_prefix} {key} data entry has trip details')
                        for trip in response_data['tripdetails']:
                            print(f'{log_prefix} Parsing trip for {key}')
                            # print(f'{routes}')
                            # print(f'{routes_children}')
                            in_key = routes[key]
                            print(f'{log_prefix} Saving route {route} with parent {in_key}')
                            original_start = datetime.strptime(f"{tomorrow_str} {trip['starttime']} +0530",
                                                               '%Y-%m-%d %H:%M %z')
                            for i in range(2):
                                buffer = i * 5
                                after = (original_start + timedelta(minutes=buffer))
                                before = (original_start - timedelta(minutes=buffer))
                                if not any((d['time'] == after and d['key'] == in_key) for d in timings):
                                    timings.append({'time': after,
                                                    'key': in_key})

                                if not any((d['time'] == before and d['key'] == in_key) for d in timings):
                                    timings.append({'time': before,
                                                    'key': in_key})
                            if not any((d['time'] == original_start and d['key'] == in_key) for d in timings):
                                timings.append({'time': original_start, 'key': in_key})
                        time.sleep(2)  # Sleep for 2 seconds after getting timing information
        update_timings_state.get()
        update_timings_state.put(False)
        while not update_timings.empty():
            update_timings.get()
        [update_timings.put(t) for t in timings]
        timings_all.get()
        timings_all.put(timings)
        update_timings_state.get()
        update_timings_state.put(True)

        pass

    async def write_loop():  # Write data_snapshot to dynamodb every 30 seconds if data_snapshot.last_update < 30 s
        try:
            await write_timings()
            update_timings_time = datetime.now() + timedelta(days=1)
            while True:
                if not state_queue.empty():
                    latest_data = state_queue.get().copy()
                    state_queue.put(latest_data)
                    if latest_data == end:
                        return
                    # print(f'{log_prefix} Data Tracking: {latest_data}')  # Write data to external source
                time.sleep(30)
                if update_timings_time < datetime.now():  # Update timings every day
                    await write_timings()
                    update_timings_time = update_timings_time + timedelta(days=1)
                # queue.put(end)
        except Exception as e:
            print(f'WRITER EXECUTION ERROR {e}')
            print(traceback.format_exc())

    asyncio.run(write_loop())


with concurrent.futures.ThreadPoolExecutor() as main_executor:
    main_executor.submit(main_runner)
    main_executor.submit(writer)

    app = Flask(__name__)


    @app.route('/')
    def index():
        log_prefix = '[API_INDEX_CALL]'
        print(f'{log_prefix} Received API call')
        current_state = state_queue.get().copy() if not state_queue.empty() else {}
        if not current_state == {}:
            print(f'{log_prefix} Returning data value')
            state_queue.put(current_state)
            return current_state['data']
        print(f'{log_prefix} Returning default data')
        return current_state


    @app.route('/info/')
    def run_info():
        log_prefix = '[API_INFO_CALL]'
        print(f'{log_prefix} Received API call')
        updating_set: set = updating.get().copy()
        updating.put(updating_set)
        print(f'{log_prefix} Received currently_updating')
        timings = timings_all.get().copy()
        timings_all.put(timings)
        print(f'{log_prefix} Received timings')
        print(f'{log_prefix} Constructing info object')
        info = {
            'currently_updating': list(updating_set),
            'currently_saved_startup_timings': [{'time': time_entry['time'].isoformat(),
                                                 'key': time_entry['key']} for time_entry in timings],
            'routes_parent': routes,
            'routes_children': routes_children
        }
        print(f'{log_prefix} Returning info object')
        return info


    app.run()

import calendar
from collections import OrderedDict
from copy import deepcopy
import difflib
import json
import os
import pprint
import sys
import uuid

from lsf import RequestStates, MachineStates, MachineResults
import cluster
from util import JsonStore, failureresponse
import util
import lsf
from cyclecliwrapper import UserError


logger = None

PLACEHOLDER_TEMPLATE = {"templateId": "exceptionPlaceholder", 
                        "maxNumber": 1,
                        "attributes": {
                            "mem": ["Numeric", 1024],
                            "ncpus": ["Numeric", 1],
                            "ncores": ["Numeric", 1]
                            }
                        }


class InvalidCycleCloudVersionError(RuntimeError):
    pass


class CycleCloudProvider:
    
    def __init__(self, config, cluster, hostnamer, json_writer, terminate_requests, templates, clock):
        self.config = config
        self.cluster = cluster
        self.hostnamer = hostnamer
        self.json_writer = json_writer
        self.terminate_json = terminate_requests
        self.templates_json = templates
        self.exit_code = 0
        self.clock = clock
        self.termination_timeout = float(self.config.get("cyclecloud.termination_request_retirement", 120) * 60)
        self.node_request_timeouts = float(self.config.get("cyclecloud.machine_request_retirement", 120) * 60)
        self.fine = False

    def _escape_id(self, name):
        return name.lower().replace("_", "")
    
    def example_templates(self):
        self._example_templates(self.templates()["templates"], [sys.stdout])
        
    def _example_templates(self, templates, writers):
        
        example = OrderedDict()
        nodearrays = []
        for template in templates["templates"]:
            nodearray = template["attributes"]["nodearray"][1]
            if nodearray not in nodearrays:
                nodearrays.append(nodearray)
        
        for nodearray in nodearrays:
            example[nodearray] = {"templateId": nodearray,
                                  "attributes": {"custom": ["String", "custom_value"]}}
            
        for writer in writers:
            json.dump(example, writer, indent=2, separators=(',', ': '))
            
    # If we return an empty list or templates with 0 hosts, it removes us forever and ever more, so _always_
    # return at least one machine.
    @failureresponse({"templates": [PLACEHOLDER_TEMPLATE], "status": RequestStates.complete_with_error})
    def templates(self):
        """
        input (ignored):
        []
        
        output:
        {'templates': [{'attributes': {'azurecchost': ['Boolean', '1'],
                               'mem': ['Numeric', '2048'],
                               'ncores': ['Numeric', '4'],
                               'ncpus': ['Numeric', '4'],
                               'type': ['String', 'X86_64'],
                               'zone': ['String', 'southeastus']},
                'instanceTags': 'group=project1',
                'maxNumber': 10,
                'pgrpName': None,
                'priority': 0,
                'templateId': 'execute0'},
               {'attributes': {'azurecchost': ['Boolean', '1'],
                               'mem': ['Numeric', '4096'],
                               'ncores': ['Numeric', '8'],
                               'ncpus': ['Numeric', '8'],
                               'type': ['String', 'X86_64'],
                               'zone': ['String', 'southeastus']},
                'instanceTags': 'group=project1',
                'maxNumber': 10,
                'pgrpName': None,
                'priority': 0,
                'templateId': 'execute1'}]}
        """
        
        prior_templates = self.templates_json.read()
        prior_templates_str = json.dumps(prior_templates, indent=2)
        
        at_least_one_available_bucket = False
        
        with self.templates_json as templates_store:
            
            # returns Cloud.Node records joined on MachineType - the array node only
            response = self.cluster.status()

            nodearrays = response["nodearrays"]
            
            if "nodeArrays" in nodearrays:
                logger.error("Invalid CycleCloud version. Please upgrade your CycleCloud instance.")
                raise InvalidCycleCloudVersionError("Invalid CycleCloud version. Please upgrade your CycleCloud instance.")
            
            if self.fine:
                logger.debug("nodearrays response\n%s", json.dumps(nodearrays, indent=2))
            
            currently_available_templates = set()
            
            default_priority = len(nodearrays) * 10
            
            for nodearray_root in nodearrays:
                nodearray = nodearray_root.get("nodearray")
                
                # legacy, ignore any dynamically created arrays.
                if nodearray.get("Dynamic"):
                    continue
                
                if "recipe[lsf::worker]" not in nodearray.get("Configuration", {}).get("run_list", []):
                    continue
                
                for bucket in nodearray_root.get("buckets"):
                    machine_type_name = bucket["definition"]["machineType"]
                    machine_type_short = machine_type_name.lower().replace("standard_", "").replace("basic_", "").replace("_", "")
                    machine_type = bucket["virtualMachine"]
                    
                    # LSF hates special characters
                    nodearray_name = nodearray_root["name"]
                    template_id = "%s%s" % (nodearray_name, machine_type_name)
                    template_id = self._escape_id(template_id)
                    currently_available_templates.add(template_id)
                    
                    max_count = self._max_count(nodearray, machine_type.get("vcpuCount"), bucket)

                    at_least_one_available_bucket = at_least_one_available_bucket or max_count > 0
                    memory = machine_type.get("memory") * 1024
                    is_low_prio = nodearray.get("Interruptible", False)
                    ngpus = 0
                    try:
                        # if the user picks a non-gpu machine ngpus will actually be defined as None.
                        ngpus = int(nodearray.get("Configuration", {}).get("lsf", {}).get("ngpus") or 0)
                    except ValueError:
                        logger.exception("Ignoring lsf.ngpus for nodearray %s" % nodearray_name)
                    
                    record = {
                        "maxNumber": max_count,
                        "templateId": template_id,
                        "priority": nodearray.get("Priority", default_priority),
                        "attributes": {
                            "zone": ["String", nodearray.get("Region")],
                            "mem": ["Numeric", memory],
                            "ncpus": ["Numeric", machine_type.get("vcpuCount")],
                            "ncores": ["Numeric", machine_type.get("vcpuCount")],
                            "azurecchost": ["Boolean", "1"],
                            "type": ["String", "X86_64"],
                            "machinetypefull": ["String", machine_type_name],
                            "machinetype": ["String", machine_type_short],
                            "nodearray": ["String", nodearray_name],
                            "azureccmpi": ["Boolean", "0"],
                            "azurecclowprio": ["Boolean", "1" if is_low_prio else "0"]
                        }
                    }
                    
                    if ngpus:
                        record["attributes"]["ngpus"] = ["Numeric", ngpus]
                    
                    # deepcopy so we can pop attributes
                    
                    for override_sub_key in ["default", nodearray_name]:
                        overrides = deepcopy(self.config.get("templates.%s" % override_sub_key, {}))
                        attribute_overrides = overrides.pop("attributes", {})
                        record.update(overrides)
                        record["attributes"].update(attribute_overrides)
                    
                    attributes = self.generate_userdata(record)
                    
                    custom_env = self._parse_UserData(record.pop("UserData", "") or "")
                    record["UserData"] = {"lsf": {}}
                    
                    if custom_env:
                        record["UserData"]["lsf"] = {"custom_env": custom_env,
                                                     "custom_env_names": " ".join(sorted(custom_env.iterkeys()))}
                    
                    record["UserData"]["lsf"]["attributes"] = attributes
                    record["UserData"]["lsf"]["attribute_names"] = " ".join(sorted(attributes.iterkeys()))
                    
                    templates_store[template_id] = record
                    
                    for n, placement_group in enumerate(_placement_groups(self.config)):
                        template_id = record["templateId"] + placement_group
                        # placement groups can't be the same across templates. Might as well make them the same as the templateid
                        namespaced_placement_group = template_id
                        if is_low_prio:
                            # not going to create mpi templates for interruptible nodearrays.
                            # if the person updated the template, set maxNumber to 0 on any existing ones
                            if template_id in templates_store:
                                templates_store[template_id]["maxNumber"] = 0
                                continue
                            else:
                                break
                        
                        record_mpi = deepcopy(record)
                        record_mpi["attributes"]["placementgroup"] = ["String", namespaced_placement_group]
                        record_mpi["UserData"]["lsf"]["attributes"]["placementgroup"] = namespaced_placement_group
                        record_mpi["attributes"]["azureccmpi"] = ["Boolean", "1"]
                        record_mpi["UserData"]["lsf"]["attributes"]["azureccmpi"] = True
                        # regenerate names, as we have added placementgroup
                        record_mpi["UserData"]["lsf"]["attribute_names"] = " ".join(sorted(record_mpi["attributes"].iterkeys()))
                        record_mpi["priority"] = record_mpi["priority"] - n - 1
                        record_mpi["templateId"] = template_id
                        record_mpi["maxNumber"] = min(record["maxNumber"], nodearray.get("Azure", {}).get("MaxScalesetSize", 40))
                        templates_store[record_mpi["templateId"]] = record_mpi
                        currently_available_templates.add(record_mpi["templateId"])
                    default_priority = default_priority - 10
            
            # for templates that are no longer available, advertise them but set maxNumber = 0
            for lsf_template in templates_store.values():
                if lsf_template["templateId"] not in currently_available_templates:
                    if self.fine:
                        logger.debug("Ignoring old template %s vs %s", lsf_template["templateId"], currently_available_templates)
                    lsf_template["maxNumber"] = 0
           
        new_templates = self.templates_json.read()
        
        new_templates_str = json.dumps(new_templates, indent=2)
        lsf_templates = list(new_templates.values())
        lsf_templates = sorted(lsf_templates, key=lambda x: -x["priority"])
        
        if new_templates_str != prior_templates_str and len(prior_templates) > 0:
            generator = difflib.context_diff(prior_templates_str.splitlines(), new_templates_str.splitlines())
            difference = "\n".join([str(x) for x in generator])
            new_template_order = ", ".join(["%s:%s" % (x.get("templateId", "?"), x.get("maxNumber", "?")) for x in lsf_templates])
            logger.warn("Templates have changed - new template priority order: %s", new_template_order)
            logger.warn("Diff:\n%s", str(difference))

        # Note: we aren't going to store this, so it will naturally appear as an error during allocation.
        if not at_least_one_available_bucket:
            lsf_templates.insert(0, PLACEHOLDER_TEMPLATE)
        
        return self.json_writer({"templates": lsf_templates}, debug_output=False)
    
    def generate_userdata(self, template):
        ret = {}
        
        for key, value_array in template.get("attributes", {}).iteritems():
            if len(value_array) != 2:
                logger.error("Invalid attribute %s %s", key, value_array)
                continue
            if value_array[0].lower() == "boolean":
                ret[key] = str(value_array[1] != "0").lower()
            else:
                ret[key] = value_array[1]
        
        if template.get("customScriptUri"):
            ret["custom_script_uri"] = template.get("customScriptUri")
            
        return ret
        
    def _parse_UserData(self, user_data):
        ret = {}
        
        user_data = (user_data or "").strip()
        
        if not user_data:
            return ret
        
        key_values = user_data.split(";")
        
        # kludge: this can be overridden either at the template level
        # or during a creation request. We always want it defined in userdata
        # though.
        
        for kv in key_values:
            try:
                key, value = kv.split("=", 1)
                ret[key] = value
            except ValueError:
                logger.error("Invalid UserData entry! '%s'", kv)
        return ret
    
    def _max_count(self, nodearray, machine_cores, bucket):
        if machine_cores < 0:
            logger.error("Invalid number of machine cores - %s", machine_cores)
            return -1
        
        max_count = bucket.get("maxCount")
        
        if max_count is not None:
            logger.debug("Using maxCount %s for %s", max_count, bucket)
            return max(-1, max_count)
        
        max_core_count = bucket.get("maxCoreCount")
        if max_core_count is None:
            if nodearray.get("maxCoreCount") is None:
                logger.error("Need to define either maxCount or maxCoreCount! %s", pprint.pformat(bucket))
                return -1
            logger.debug("Using maxCoreCount")
            max_core_count = nodearray.get("maxCoreCount")
        
        max_core_count = max(-1, max_core_count)
        
        return max_core_count / machine_cores
    
    @failureresponse({"requests": [], "status": RequestStates.running})
    def create_machines(self, input_json):
        """
        input:
        {'rc_account': 'default',
         'template': {'machineCount': 1, 'templateId': 'execute0'},
         'user_data': {}}

        output:
        {'message': 'Request VM from Azure CycleCloud successful.',
         'requestId': 'req-123'}
        """
        request_id = str(uuid.uuid4())
        
        try:
            template_store = self.templates_json.read()
        
            # same as nodearrays - Cloud.Node joined with MachineType
            template_id = input_json["template"]["templateId"]
            template = template_store.get(template_id)
            
            if not template:
                available_templates = template_store.keys()
                return self.json_writer({"requestId": request_id, "status": RequestStates.complete_with_error, 
                                        "message": "Unknown templateId %s. Available %s" % (template_id, available_templates)})
                
            machine_count = input_json["template"]["machineCount"]
            
            def _get(name):
                return template["attributes"].get(name, [None, None])[1]
            
            rc_account = input_json.get("rc_account", "default")
            
            user_data = template.get("UserData")

            if rc_account != "default":
                if "lsf" not in user_data:
                    user_data["lsf"] = {}
                
                if "custom_env" not in user_data["lsf"]:
                    user_data["lsf"]["custom_env"] = {}
                    
                user_data["lsf"]["custom_env"]["rc_account"] = rc_account
                user_data["lsf"]["custom_env_names"] = " ".join(sorted(user_data["lsf"]["custom_env"].keys()))
            
            nodearray = _get("nodearray")
            
            machinetype_name = _get("machinetypefull")
            
            request_set = {'count': machine_count,
                       'definition': {'machineType': machinetype_name},
                       'nodeAttributes': {'Tags': {"rc_account": rc_account},
                                          'Configuration': user_data},
                       'nodearray': nodearray}
            if template["attributes"].get("placementgroup"):
                request_set["placementGroupId"] = template["attributes"].get("placementgroup")[1]
                       
            self.cluster.add_nodes({'requestId': request_id,
                                    'sets': [request_set]})
            if template["attributes"].get("placementgroup"):
                logger.info("Requested %s instances of machine type %s in placement group %s for nodearray %s.", machine_count, machinetype_name, _get("placementgroup"), _get("nodearray"))
            else:
                logger.info("Requested %s instances of machine type %s in nodearray %s.", machine_count, machinetype_name, _get("nodearray"))
            
            return self.json_writer({"requestId": request_id, "status": RequestStates.running,
                                     "message": "Request instances success from Azure CycleCloud."})
        except UserError as e:
            logger.exception("Azure CycleCloud experienced an error and the node creation request failed. %s", e)
            return self.json_writer({"requestId": request_id, "status": RequestStates.complete_with_error,
                                     "message": "Azure CycleCloud experienced an error: %s" % unicode(e)})
        except ValueError as e:
            logger.exception("Azure CycleCloud experienced an error and the node creation request failed. %s", e)
            return self.json_writer({"requestId": request_id, "status": RequestStates.complete_with_error,
                                     "message": "Azure CycleCloud experienced an error: %s" % unicode(e)})
        except Exception as e:
            logger.exception("Azure CycleCloud experienced an error, though it may have succeeded: %s", e)
            return self.json_writer({"requestId": request_id, "status": RequestStates.running,
                                     "message": "Azure CycleCloud experienced an error, though it may have succeeded: %s" % unicode(e)})
            
    @failureresponse({"requests": [], "status": RequestStates.running})
    def _create_status(self, input_json):
        """
        input:
        {'requests': [{'requestId': 'req-123'}, {'requestId': 'req-234'}]}

    
        output:
        {'message': '',
         'requests': [{'machines': [{'launchtime': 1516131665,
                                     'machineId': 'id-123',
                                     'message': '',
                                     'privateDnsAddress': '
                                     'name': 'execute-5',
                                     'privateIpAddress': '10.0.1.23',
                                     'result': 'succeed',
                                     'status': 'running'}],
                       'message': '',
                       'requestId': 'req-123',
                       'status': 'complete'}],
         'status': 'complete'}

        """
        request_status = RequestStates.complete
        
        request_ids = [r["requestId"] for r in input_json["requests"]]
        try:
            nodes_by_request_id = self.cluster.nodes(request_ids=request_ids)
        except UserError as e:
            logger.exception("Azure CycleCloud experienced an error and the node creation request failed. %s", e)
            return self.json_writer({"status": RequestStates.complete_with_error,
                                    "requests": [{"requestId": request_id, "status": RequestStates.complete_with_error} for request_id in request_ids] ,
                                     "message": "Azure CycleCloud experienced an error: %s" % unicode(e)})
        except ValueError as e:
            logger.exception("Azure CycleCloud experienced an error and the node creation request failed. %s", e)
            return self.json_writer({"requestId": request_id, "status": RequestStates.complete_with_error,
                                     "message": "Azure CycleCloud experienced an error: %s" % unicode(e)})
        
        message = ""
        
        response = {"requests": []}
        
        unknown_state_count = 0
        requesting_count = 0
        
        for request_id, requested_nodes in nodes_by_request_id.iteritems():
            if not requested_nodes:
                # nothing to do.
                logger.warn("No nodes found for request id %s.", request_id)
            
            machines = []
            request = {"requestId": request_id,
                        "machines": machines}
            
            response["requests"].append(request)
            
            for node in requested_nodes["nodes"]:
                # for new nodes, completion is Ready. For "released" nodes, as long as
                # the node has begun terminated etc, we can just say success.
                node_status = node.get("State")
                node_target_state = node.get("TargetState", "Started")
                
                machine_status = MachineStates.active
                
                hostname = None
                private_ip_address = None
                 
                if node_target_state != "Started":
                    unknown_state_count = unknown_state_count + 1
                    continue
                
                elif node_status in ["Unavailable", "Failed"]:
                    machine_result = MachineResults.failed
                    machine_status = MachineStates.error
                    if request_status != RequestStates.running:
                        message = node.get("StatusMessage", "Unknown error.")
                        request_status = RequestStates.complete_with_error
        
                elif not node.get("InstanceId"):
                    requesting_count = requesting_count + 1
                    request_status = RequestStates.running
                    continue
                
                elif node_status == "Started":
                    machine_result = MachineResults.succeed
                    machine_status = MachineStates.active
                    private_ip_address = node.get("PrivateIp")
                    if not private_ip_address:
                        logger.warn("No ip address found for ready node %s", node.get("Name"))
                        machine_result = MachineResults.executing
                        machine_status = MachineStates.building
                    else:
                        hostname = self.hostnamer.hostname(private_ip_address)
                else:
                    machine_result = MachineResults.executing
                    machine_status = MachineStates.building
                    request_status = RequestStates.running
                
                machine = {
                    "name": hostname,
                    "status": machine_status,
                    "result": machine_result,
                    "machineId": node.get("NodeId"),
                    # maybe we can add something so we don"t have to expose this
                    # node["PhaseMap"]["Cloud.AwaitBootup"]["StartTime"]["$date"]
                    "launchtime": node.get("LaunchTime"),
                    "privateIpAddress": private_ip_address,
                    "message": node.get("StatusMessage")
                }
                
                machines.append(machine)
            
            active = len([x for x in machines if x["status"] == MachineStates.active])
            building = len([x for x in machines if x["status"] == MachineStates.building])
            failed = len([x for x in machines if x["status"] == MachineStates.error])
            
            logger.info("Machine states for requestId %s: %d active, %d building, %d requesting, %d failed and %d in an unknown state.", 
                        request_id, active, building, requesting_count, failed, unknown_state_count)
            
            request["status"] = request_status
            if request_status == RequestStates.complete:
                logger.info("Request %s is complete.", request_id)
            elif request_status == RequestStates.complete_with_error:
                logger.warn("Request %s completed with error: %s.", request_id, message)
            request["message"] = message
        
        response["status"] = lsf.RequestStates.complete
        
        return self.json_writer(response)
        
    @failureresponse({"requests": [], "status": RequestStates.running})
    def _deperecated_terminate_status(self, input_json):
        # can transition from complete -> executing or complete -> complete_with_error -> executing
        # executing is a terminal state.
        request_status = RequestStates.complete
        
        response = {"requests": []}
        # needs to be a [] when we return
        with self.terminate_json as terminate_requests:
            
            self._cleanup_expired_requests(terminate_requests, self.termination_timeout, "terminated")
            
            termination_ids = [r["requestId"] for r in input_json["requests"] if r["requestId"]]
            try:
                machines_to_terminate = []
                for termination_id in termination_ids:
                    if termination_id in terminate_requests:
                        termination = terminate_requests[termination_id]
                        if not termination.get("terminated"):
                            for machine_id, name in termination["machines"].iteritems():
                                machines_to_terminate.append({"machineId": machine_id, "name": name})
                
                if machines_to_terminate:
                    logger.warn("Re-attempting termination of nodes %s", machines_to_terminate)
                    self.cluster.terminate(machines_to_terminate, self.hostnamer)
                    
                for termination_id in termination_ids:
                    if termination_id in terminate_requests:
                        termination = terminate_requests[termination_id]
                        termination["terminated"] = True
                        
            except Exception:
                request_status = RequestStates.running
                logger.exception("Could not terminate nodes with ids %s. Will retry", machines_to_terminate)
            
            for termination_id in termination_ids:
                response_machines = []
                request = {"requestId": termination_id,
                           "machines": response_machines}
            
                response["requests"].append(request)
                
                if termination_id in terminate_requests:
                    termination_request = terminate_requests.get(termination_id)
                    machines = termination_request.get("machines", {})
                    
                    if machines:
                        logger.info("Terminating machines: %s", [hostname for hostname in machines.itervalues()])
                    else:
                        logger.warn("No machines found for termination request %s. Will retry.", termination_id)
                        request_status = RequestStates.running
                    
                    for machine_id, hostname in machines.iteritems():
                        response_machines.append({"name": hostname,
                                                   "status": MachineStates.deleted,
                                                   "result": MachineResults.succeed,
                                                   "machineId": machine_id})
                else:
                    # we don't recognize this termination request!
                    logger.warn("Unknown termination request %s. You may intervene manually by updating terminate_nodes.json" + 
                                 " to contain the relevant NodeIds. %s ", termination_id, terminate_requests)
                    # set to running so lsf will keep retrying, hopefully, until someone intervenes.
                    request_status = RequestStates.running
                    request["message"] = "Unknown termination request id."
               
                request["status"] = request_status
        
        response["status"] = request_status
        
        return self.json_writer(response)
    
    @failureresponse({"status": RequestStates.running})
    def terminate_status(self, input_json):
        ids_to_hostname = {}
    
        for machine in input_json["machines"]:
            ids_to_hostname[machine["machineId"]] = machine["name"]
        
        with self.terminate_json as term_requests:
            requests = {}
            for node_id, hostname in ids_to_hostname.iteritems():
                machine_record = {"machineId": node_id, "name": hostname}
                found_a_request = False
                for request_id, request in term_requests.iteritems():
                    if node_id in request["machines"]:
                        
                        found_a_request = True
                        
                        if request_id not in requests:
                            requests[request_id] = {"machines": []}
                        
                        requests[request_id]["machines"].append(machine_record)
                
                if not found_a_request:
                    logger.warn("No termination request found for machine %s", machine_record)
            
            deprecated_json = {"requests": [{"requestId": request_id, "machines": requests[request_id]["machines"]} for request_id in requests]}
            return self._deperecated_terminate_status(deprecated_json)
        
    def _cleanup_expired_requests(self, requests, retirement, completed_key):
        now = calendar.timegm(self.clock())
        for req_id in list(requests.keys()):
            try:
                request = requests[req_id]
                request_time = request.get("requestTime", -1)
                
                if request_time < 0:
                    logger.info("Request has no requestTime")
                    request["requestTime"] = request_time = now
                
                if not request.get(completed_key):
                    logger.info("Request has not completed, ignoring expiration: %s", request)
                    continue
                
                # in case someone puts in a string manuall
                request_time = float(request_time)
                    
                if (now - request_time) > retirement:
                    logger.debug("Found retired request %s", request)
                    requests.pop(req_id)
                
            except Exception:
                logger.exception("Could not remove stale request %s", req_id)
                
    @failureresponse({"status": RequestStates.complete_with_error})
    def terminate_machines(self, input_json):
        """
        input:
        {
            "machines":[ {"name": "host-123", "machineId": "id-123"} ]
        }
        
        output:
        {
            "message" : "Delete VM success.",
            "requestId" : "delete-i-123",
            "status": "complete"
        }
        """
        request_id = "delete-%s" % str(uuid.uuid4())
        request_id_persisted = False
        try:
            with self.terminate_json as terminations:
                machines = {}
                for machine in input_json["machines"]:
                    machines[machine["machineId"]] = machine["name"]
                    
                terminations[request_id] = {"id": request_id, "machines": machines, "requestTime": calendar.timegm(self.clock())}
            
            request_id_persisted = True
            request_status = RequestStates.complete
            message = "CycleCloud is terminating the VM(s)",

            try:
                self.cluster.terminate(input_json["machines"], self.hostnamer)
                with self.terminate_json as terminations:
                    terminations[request_id]["terminated"] = True
            except Exception:
                # set to running, we will retry on any status call anyways.
                request_status = RequestStates.running
                message = str(message)
                logger.exception("Could not terminate %s", machines.keys())
            
            logger.info("Terminating %d machine(s): %s", len(machines), machines.keys())
            
            return self.json_writer({"message": message,
                                     "requestId": request_id,
                                     "status": request_status
                                     })
        except Exception as e:
            logger.exception(unicode(e))
            if request_id_persisted:
                return self.json_writer({"status": RequestStates.running, "requestId": request_id})
            return self.json_writer({"status": RequestStates.complete_with_error, "requestId": request_id, "message": unicode(e)})
        
    def status(self, input_json):
        '''
        Kludge: can't seem to get provider.json to reliably call the correct request action.
        '''
        json_writer = self.json_writer
        self.json_writer = lambda x: x
        creates = [x for x in input_json["requests"] if not x["requestId"].startswith("delete-")]
        deletes = [x for x in input_json["requests"] if x["requestId"].startswith("delete-")]
        create_response = {}
        delete_response = {}
        
        if creates:
            create_response = self._create_status({"requests": creates})
            assert "status" in create_response
        if deletes:
            delete_response = self._deperecated_terminate_status({"requests": deletes})
            assert "status" in delete_response
        
        create_status = create_response.get("status", RequestStates.complete)
        delete_status = delete_response.get("status", RequestStates.complete)
        
        # if either are still running, then we need to mark it as running so this will continued
        # to be called
        if RequestStates.running in [create_status, delete_status]:
            combined_status = RequestStates.running
        # if one completed with error, then they both did.
        elif RequestStates.complete_with_error in [create_status, delete_status]:
            combined_status = RequestStates.complete_with_error
        else:
            combined_status = RequestStates.complete
        
        response = {"status": combined_status,
                    "requests": create_response.get("requests", []) + delete_response.get("requests", [])
                    }
        return json_writer(response)
    

def _placement_groups(config):
    try:
        num_placement_groups = min(26 * 26, int(config.get("lsf.num_placement_groups", 0)))
    except ValueError:
        raise ValueError("Expected a positive integer for lsf.num_placement_groups, got %s" % config.get("lsf.num_placement_groups"))
    if num_placement_groups <= 0:
        return []
    else:
        return ["pg%s" % x for x in xrange(num_placement_groups)]


def simple_json_writer(data, debug_output=True):  # pragma: no cover
    data_str = json.dumps(data)
    if debug_output:
        logger.debug("Response: %s", data_str)
    print data_str
    return data


def true_gmt_clock():  # pragma: no cover
    import time
    return time.gmtime()


def main(argv=sys.argv, json_writer=simple_json_writer):  # pragma: no cover
    try:
        
        global logger
        provider_config, logger, fine = util.provider_config_from_environment()
        
        data_dir = os.getenv('PRO_DATA_DIR', os.getcwd())
        hostnamer = util.Hostnamer(provider_config.get("cyclecloud.hostnames.use_fqdn", True))
        cluster_name = provider_config.get("cyclecloud.cluster.name")
        
        provider = CycleCloudProvider(config=provider_config,
                                      cluster=cluster.Cluster(cluster_name, provider_config, logger),
                                      hostnamer=hostnamer,
                                      json_writer=json_writer,
                                      terminate_requests=JsonStore("terminate_requests.json", data_dir),
                                      templates=JsonStore("templates.json", data_dir, formatted=True),
                                      clock=true_gmt_clock)
        provider.fine = fine

        # every command has the format cmd -f input.json        
        cmd, ignore, input_json_path = argv[1:]

        input_json = util.load_json(input_json_path)
        
        if provider.fine:
            logger.debug("Arguments - %s %s %s", cmd, ignore, json.dumps(input_json))
                
        if cmd == "templates":
            provider.templates()
        elif cmd == "create_machines":
            provider.create_machines(input_json)
        elif cmd in ["status", "create_status", "terminate_status"]:
            if "requests" in input_json:
                # provider.status handles both create_status and deprecated terminate_status calls.
                provider.status(input_json)
            elif cmd == "terminate_status":
                # doesn't pass in a requestId but just a list of machines.
                provider.terminate_status(input_json)
            else:
                # should be impossible
                raise RuntimeError("Unexpected input json for cmd %s" % (input_json, cmd))
        elif cmd == "terminate_machines":
            provider.terminate_machines(input_json)
            
    except ImportError as e:
        logger.exception(unicode(e))

    except Exception as e:
        if logger:
            logger.exception(unicode(e))
        else:
            import traceback
            traceback.print_exc()
            

if __name__ == "__main__":
    main()  # pragma: no cover
else:
    logger = util.init_logging()

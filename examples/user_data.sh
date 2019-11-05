#!/bin/bash
set -x

LSF_TOP_LOCAL=$(jetpack config lsf.lsf_top)
MASTER_HOSTS_STRING=" ip-0A05000B "

echo "master managed externally, LSF_TOP is local edit lsf.conf in place."
LSF_CONF="$LSF_TOP_LOCAL/conf/lsf.conf"
sed -i "s/LSF_SERVER_HOSTS=.*/LSF_SERVER_HOSTS=\"${MASTER_HOSTS_STRING}\"/g" ${LSF_TOP_LOCAL}/conf/lsf.conf

set +e
source $LSF_TOP/conf/profile.lsf
set -e

# Default LSF Environment Variables
# rc_account
# template_id 
# providerName (default: cyclecloud)
# clustername cyclecloud
# cyclecloud_nodeid

# Custom LSF Environment Variables
# placement_group_id
# nodearray_name

# set LSF_LOCAL_RESOURCES
sed -i '/LSF_LOCAL_RESOURCES/d' $LSF_CONF

TEMP_LOCAL_RESOURCES=" [resource cyclecloudhost] "
if [ -n "${rc_account}" ]; then
  TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resourcemap ${rc_account}*rc_account]"
fi

if [ -n "${cyclecloud_nodeid}" ]; then
  TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resourcemap ${cyclecloud_nodeid}*instanceID]"
fi

if [ -n "${template_id}" ]; then
  TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resourcemap ${template_id}*templateID]"
fi

if [ -n "${clustername}" ]; then
  TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resourcemap ${clustername}*clusterName]"
fi

if [ -n "${placement_group_id}" ]; then
  TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resourcemap ${placement_group_id}*placementgroup]"
fi

echo "LSF_LOCAL_RESOURCES=\"${TEMP_LOCAL_RESOURCES}\"" >> $LSF_CONF

lsadmin limstartup 
lsadmin resstartup 
badmin hstartup 

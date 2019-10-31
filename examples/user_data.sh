#!/bin/bash
set -x

LSF_TOP_LOCAL=$(jetpack config lsf.lsf_top)
MASTER_HOSTS_STRING=" ip-0A05000B "

echo "master managed externally, LSF_TOP is local edit lsf.conf in place."
LSF_CONF="$LSF_TOP_LOCAL/conf/lsf.conf"
LSF_ENVDIR_LOCAL="$LSF_TOP_LOCAL"
sed -i "s/LSF_SERVER_HOSTS=.*/LSF_SERVER_HOSTS=\"${MASTER_HOSTS_STRING}\"/g" ${LSF_TOP_LOCAL}/conf/lsf.conf


# Default LSF Environment Variables
# rc_account
# template_id 
# providerName (default: cyclecloud)
# clustername cyclecloud
# cyclecloud_nodeid

# Custom LSF Environment Variables
# placement_group_id
# nodearray_name

# set LSF_SERVER_HOSTS


# set LSF_LOCAL_RESOURCES
sed -i '/LSF_LOCAL_RESOURCES/d' $LSF_CONF

# assumes templateId == placementGroupName (One template for each placement group)
# nodearrayname == jetpack config cyclecloud.template
TEMP_LOCAL_RESOURCES="[resourcemap ${rc_account}*rc_account] [resource cyclecloudhost] [resourcemap ${nodearray_name}*nodearray]"
if [[ $template_id == ondemandmpi* ]]; then
    TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resource cyclecloudmpi] [resourcemap ${cyclecloud_nodeid}*instanceid] [resourcemap ${placement_group_id}*placementgroup]"
elif [[ $template_id == gpumpi* ]]; then
    TEMP_LOCAL_RESOURCES="$TEMP_LOCAL_RESOURCES [resource cyclecloudmpi] [resourcemap ${cyclecloud_nodeid}*instanceid] [resourcemap ${placement_group_id}*placementgroup]"
fi

echo "LSF_LOCAL_RESOURCES=\"${TEMP_LOCAL_RESOURCES}\"" >> $LSF_CONF

set +e
source $LSF_TOP_LOCAL/conf/profile.lsf
set -e
#export LSF_ENVDIR=$LSF_ENVDIR_LOCAL
# point processes to the new lsf.local_etc
lsadmin limstartup -f
lsadmin resstartup -f
badmin hstartup -f

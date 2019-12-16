
include_recipe "lsf::default"
#lsf10.1_linux2.6-glibc2.3-x86_64.tar.Z	lsfsce10.2.0.6-x86_64.tar.gz
#lsf10.1_lsfinstall_linux_x86_64.tar.Z
tar_dir = node['lsf']['tar_dir']
lsf_top = node['lsf']['lsf_top']
lsf_version = node['lsf']['version']
lsf_kernel = node['lsf']['kernel']
lsf_arch = node['lsf']['arch']
clustername = node['lsf']['clustername']
entitled_install = node['lsf']['entitled_install']

lsf_product = "lsf#{lsf_version}_#{lsf_kernel}-#{lsf_arch}"
lsf_product_fp9 = "lsf#{lsf_version}_#{lsf_kernel}-#{lsf_arch}-532214"

lsf_install = "lsf#{lsf_version}_lsfinstall_linux_#{lsf_arch}"

jetpack_download "#{lsf_install}.tar.Z" do
    project "lsf"
    dest tar_dir
    not_if { ::File.exist?("#{tar_dir}/#{lsf_install}.tar.Z") }
end

jetpack_download "#{lsf_product}.tar.Z" do
    project "lsf"
    dest tar_dir
    not_if { ::File.exist?("#{tar_dir}/#{lsf_product}.tar.Z") }
end

jetpack_download "#{lsf_product_fp9}.tar.Z" do
    project "lsf"
    dest tar_dir
    only_if { entitled_install }
    not_if { ::File.exist?("#{tar_dir}/#{lsf_product_fp9}.tar.Z") }
end

#jetpack_download "#{lsf_product_rc_patch}.tar.Z" do
#    project "lsf"
#    dest tar_dir
#    only_if { entitled_install }
#    not_if { ::File.exist?("#{tar_dir}/#{lsf_product_rc_patch}.tar.Z") }
#end

jetpack_download "lsf_std_entitlement.dat" do
    project "lsf"
    dest tar_dir
    only_if { entitled_install }
    not_if { ::File.exist?("#{tar_dir}/lsf_std_entitlement.dat") }
end

execute "untar_installers" do 
    command "gunzip #{lsf_install}.tar.Z && tar -xf #{lsf_install}.tar"
    cwd tar_dir
    not_if { ::File.exist?("#{tar_dir}/lsf#{lsf_version}_lsfinstall/lsfinstall") }
end

template "#{tar_dir}/lsf#{lsf_version}_lsfinstall/lsf.install.config" do
    source 'conf/install.config.erb'
    variables lazy {{
      :master_list => node[:lsf][:master_list].nil? ? node[:hostname] : node[:lsf][:master_list]
    }}
end

execute "anf_fix_lsfprechkfuncs" do
    # update install command to ignore .snapshot dir on ANF
    command "sed -i 's/grep -v lost+found/grep -v lost+found | grep -v .snapshot/g' instlib/lsfprechkfuncs.sh"
    cwd "#{tar_dir}/lsf#{lsf_version}_lsfinstall"
    not_if { ::File.exist?("#{lsf_top}/#{lsf_version}/#{lsf_kernel}-#{lsf_arch}/lsf_release")}
    not_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}")}
    only_if { ::File.exist?("#{lsf_top}/.snapshot")}
end


yum_package "java-1.8.0-openjdk.x86_64" do
    action "install"
    not_if "yum list installed java-1.8.0-openjdk.x86_64"
  end

execute "run_lsfinstall_fp9" do
    command " . conf/profile.lsf && ./#{lsf_version}/install/patchinstall --silent #{tar_dir}/#{lsf_product_fp9}.tar.Z"
    cwd "#{lsf_top}"
    only_if { entitled_install }
    only_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}")}
    only_if { ::File.exist?("#{lsf_top}/conf/profile.lsf")}
    not_if  " . conf/profile.lsf && ./#{lsf_version}/install/pversions | grep 532214", :cwd => "#{lsf_top}"
    action :nothing
end

execute "run_lsfinstall" do
    command "./lsfinstall -f lsf.install.config"
    cwd "#{tar_dir}/lsf#{lsf_version}_lsfinstall"
    creates "#{lsf_top}/conf/profile.lsf"
    not_if { ::File.exist?("#{lsf_top}/#{lsf_version}/#{lsf_kernel}-#{lsf_arch}/lsf_release")}
    not_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}")}
    notifies :run, 'execute[run_lsfinstall_fp9]', :immediately
end

#execute "run_lsfinstall_rc_patch" do
#    command " . conf/profile.lsf && ./#{lsf_version}/install/patchinstall --silent #{tar_dir}/#{lsf_product_rc_patch}.tar.Z"
#    cwd "#{lsf_top}"
#    only_if { entitled_install }
#    not_if  " . conf/profile.lsf &&  ./#{lsf_version}/install/pversions | grep 529611", :cwd => "#{lsf_top}"
#    only_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}")}
#    not_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}/resource_connector/cyclecloud")}
#end

#execute "set_permissions_not_entitled" do
#    command "chown -R root:root #{lsf_top} && chmod 4755 #{lsf_top}/10.1/linux*/bin/*admin && touch #{lsf_top}/conf/cyclefixperms"
#    not_if { entitled_install }
#    only_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}")}
#    not_if { ::File.exist?("#{lsf_top}/conf/cyclefixperms")}
#end
#
#execute "set_permissions_entitled" do
#    command "chown -R root:root #{lsf_top} && chmod 4755 #{lsf_top}/10.1/linux*/bin/*admin && touch #{lsf_top}/conf/cyclefixperms"
#    only_if { entitled_install }
#    only_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}")}
#    only_if { ::Dir.exist?("#{lsf_top}/#{lsf_version}/resource_connector/cyclecloud")}
#    not_if { ::File.exist?("#{lsf_top}/conf/cyclefixperms")}
#end

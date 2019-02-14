# check_docker
The script listens and exposes events of docker to Zabbix

It sends data to Zabbix via zabbix_sender. You have to configure discovery rule in Zabbix in order to receive data from the script. See `Template_Docker_Basic` in `examples/docker_templates.xml`.

The script must be launched as a daemon. You have an example of SysV init-script in `examples/init_script.sh`.

Execute `check_docker --help` to find out available parameters which you can define as cli parameters or in config.
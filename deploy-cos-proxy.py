from collections import defaultdict
import os
import sys

import click
import yaml


JUJU_USER = "admin"

REQUIRED_COS_INTERFACES = [
    "grafana-dashboard",
    "logging",
    "metrics-endpoint",
    "receive-remote-write",
    "configurable-scrape-jobs",
]

# APPs shipping dashboards
DASHBOARDS = [
    "telegraf",
    "etcd",
    "ceph-dashboard",
    "prometheus-grok-exporter",
    "prometheus-libvirt-exporter",
    "prometheus-openstack-exporter",
    # Until https://github.com/canonical/cos-proxy-operator/issues/41 is not fixed
    # not importing k8s dashboards.
    # "kubernetes-master",
    # "kubernetes-control-plane",
]

DASHBOARDS_RELATION = {
    "telegraf": "dashboards",
    "etcd": "grafana",
    "ceph-dashboard": "grafana-dashboard",
    "prometheus-grok-exporter": "dashboards",
    "prometheus-libvirt-exporter": "dashboards",
    "prometheus-openstack-exporter": "dashboards",
    # "kubernetes-master": "grafana",
    # "kubernetes-control-plane": "grafana",
}

# APPs providing nrpe checks via monitors relation
MONITORS = ["nrpe"]

MONITORS_RELATIONS = {
    "nrpe": "monitors",
}

# APPs providing prometheus-targets
PROM_TARGETS = [
    "telegraf",
    "ceph-mon",
    "prometheus-grok-exporter",
    "prometheus-libvirt-exporter",
    "prometheus-openstack-exporter",
    # Until https://github.com/canonical/cos-proxy-operator/issues/41 is not fixed
    # can't add prometheus targets for k8s.
    # "kubernetes-master",
    # "kubernetes-control-plane",
]

# Name of relation with prometheus
PROM_TARGETS_RELATIONS = {
    "telegraf": "prometheus-client",
    "ceph-mon": "prometheus",
    "prometheus-grok-exporter": "prometheus-client",
    "prometheus-libvirt-exporter": "scrape",
    "prometheus-openstack-exporter": "prometheus-openstack-exporter-service",
    # "kubernetes-master": "prometheus-manual",
    # "kubernetes-control-plane": "prometheus-manual",
}

LOGGING = [
    "filebeat"
]

LOGGING_RELATIONS = {
    "filebeat": "logstash"
}

CHARMS_TO_REFRESH = [
    "prometheus-grok-exporter",
    "prometheus-libvirt-exporter",
    "prometheus-openstack-exporter"
]

def get_controller(jsfy):
    return jsfy["model"]["controller"]


def get_model(jsfy):
    return jsfy["model"]["name"]


def find_apps_from_charm(charm, jsfy):
    model_apps = jsfy["applications"]
    return {charm: [app for app in model_apps.keys()
                    if charm in model_apps[app]['charm']]}


def get_cos_offers(cos_jsfy):
    model = get_model(cos_jsfy)
    offers = []
    for offer in cos_jsfy["offers"]:
        for endopoint in cos_jsfy["offers"][offer]["endpoints"].keys():
            if endopoint in REQUIRED_COS_INTERFACES:
                offers.append(f"{JUJU_USER}/{model}.{offer}")
    return offers


def get_apps_from_list(main_jsfy, charm_list):
    result = {}
    for charm in charm_list:
        result.update(find_apps_from_charm(charm, main_jsfy))
    return result

def get_cloud_series(main_jsfy):
    machines = main_jsfy["machines"]
    stats = defaultdict(lambda: 0)
    # this ignores LXDs
    for id in machines:
        series = machines[id]["series"]
        stats[series] += 1
    return max(stats, key=stats.get)

def get_monitors_apps(main_jsfy):
    return get_apps_from_list(main_jsfy, MONITORS)


def get_dashboards_apps(main_jsfy):
    return get_apps_from_list(main_jsfy, DASHBOARDS)


def get_logging_apps(main_jsfy):
    return get_apps_from_list(main_jsfy, LOGGING)


def get_prom_targets_apps(main_jsfy):
    return get_apps_from_list(main_jsfy, PROM_TARGETS)


def load_jsfy(file_path):
    try:
        with open(file_path) as myjsfy:
            return yaml.safe_load(myjsfy)
    except IOError:
        print(f"Unable to load file {file_path}. "
              f"Please colect a fresh copy of juju status --format yaml > {file_path}")
        sys.exit(1)


@click.command()
@click.option("--cos-jsfy", "-c", required=True,
              help="Juju status in yaml format from cos model")
@click.option("--main-jsfy", "-m", required=True,
              help="Juju status in yaml format from main model")
@click.option("--secondary-jsfy-list", "-s", required=False,
              help="Juju status files in yaml format from secondary models, e.g. controller_jsfy,maas-infra_jsfy,lma_jsfy")
@click.option("--to-cos-proxy", "-t", required=True,
              help="Where to deploy the cos-proxies (i.e. lxd:0)")
@click.option("--cos-proxy-channel", default="edge",
              help="Channel of the cos-proxy charm (default: edge)")
def get_ap(cos_jsfy, main_jsfy, secondary_jsfy_list, to_cos_proxy, cos_proxy_channel):

    cos_jsfy = load_jsfy(cos_jsfy)
    main_jsfy = load_jsfy(main_jsfy)

    if secondary_jsfy_list:
        secondary_jsfy = []
        for jsfy in secondary_jsfy_list.split(","):
            secondary_jsfy.append(load_jsfy(jsfy))

    microk8s_controller = get_controller(cos_jsfy)

    action_plan = []
    cos_offers = get_cos_offers(cos_jsfy)
    monitors_apps = get_monitors_apps(main_jsfy)
    dashboards_apps = get_dashboards_apps(main_jsfy)
    prometheus_apps = get_prom_targets_apps(main_jsfy)
    logging_apps = get_logging_apps(main_jsfy)
    cloud_series = get_cloud_series(main_jsfy)
    main_controller_name = get_controller(main_jsfy)
    main_model_name = get_model(main_jsfy)
    action_plan.append(f"juju deploy ch:cos-proxy --channel {cos_proxy_channel} --to {to_cos_proxy} --bind=oam-space --series jammy cos-proxy-monitors ")
    action_plan.append(f"juju deploy ch:cos-proxy --channel {cos_proxy_channel} --to {to_cos_proxy} --bind=oam-space --series jammy")

    # TODO: juju deploy grafana-agent grafana-agent-cos-proxy-monitors --series jammy
    # TODO: juju deploy grafana-agent grafana-agent-cos-proxy --series jammy

    for offer in cos_offers:
        action_plan.append(f"juju consume {microk8s_controller}:{offer} cos-{offer.split('.')[-1]}")

    action_plan.append(f"juju offer -c {main_controller_name} admin/{main_model_name}.cos-proxy-monitors:monitors")

    action_plan.append("# wait for the model to settle")
    action_plan.append("")

    action_plan.append("juju add-relation cos-proxy:downstream-grafana-dashboard cos-grafana-dashboards:grafana-dashboard")

    action_plan.append("juju add-relation cos-proxy:downstream-prometheus-scrape cos-scrape-interval-config-metrics:configurable-scrape-jobs")

    action_plan.append("")

    for app in monitors_apps:
        relation = MONITORS_RELATIONS[app]
        for charm in monitors_apps[app]:
            cmd = f"juju add-relation cos-proxy-monitors:monitors {charm}:{relation}"
            action_plan.append(cmd)
    action_plan.append("")

    for app in dashboards_apps:
        relation = DASHBOARDS_RELATION[app]
        for charm in dashboards_apps[app]:
            cmd = f"juju remove-relation grafana:dashboards {charm}:{relation}"
            action_plan.append(cmd)
    action_plan.append("")

    # upgrade exporter charms
    action_plan.append("# wait for the model to settle")
    action_plan.append("")
    for charm in CHARMS_TO_REFRESH:
        cmd = f"juju refresh --switch ch:{charm} --channel latest/stable {charm}"
        action_plan.append(cmd)
    action_plan.append("")
    action_plan.append("# wait for both cos-proxy units to settle")
    action_plan.append("")

    for app in dashboards_apps:
        relation = DASHBOARDS_RELATION[app]
        for charm in dashboards_apps[app]:
            cmd = f"juju add-relation cos-proxy:dashboards {charm}:{relation}"
            action_plan.append(cmd)

    for app in prometheus_apps:
        relation = PROM_TARGETS_RELATIONS[app]
        for charm in prometheus_apps[app]:
            cmd = f"juju add-relation cos-proxy:prometheus-target {charm}:{relation}"
            action_plan.append(cmd)
    action_plan.append("")

    # Skip the cos-proxy <-> filebeat relation on focal and newer.
    if cloud_series in ["bionic", "xenial"]:
        action_plan.append("juju add-relation cos-proxy:downstream-logging cos-loki-logging:logging")
        for app in logging_apps:
            relation = LOGGING_RELATIONS[app]
            for charm in logging_apps[app]:
                cmd = f"juju add-relation cos-proxy:filebeat {charm}:{relation}"
                action_plan.append(cmd)
        action_plan.append("")

    # for each secondary model set up only nrpe monitoring
    if secondary_jsfy_list:
        for jsfy in secondary_jsfy:
            controller_name = get_controller(jsfy)
            model_name = get_model(jsfy)

            action_plan.append(f"export JUJU_CONTROLLER={controller_name}")
            action_plan.append(f"export JUJU_MODEL={model_name}")
            action_plan.append(f"juju consume {main_controller_name}:admin/{main_model_name}.cos-proxy-monitors cos-proxy-monitors")

            monitors_apps = get_monitors_apps(jsfy)
            for app in monitors_apps:
                relation = MONITORS_RELATIONS[app]
                for charm in monitors_apps[app]:
                    cmd = f"juju add-relation cos-proxy-monitors:monitors {charm}:{relation}"
                    action_plan.append(cmd)
            action_plan.append("")

    action_plan.append("# wait for 'juju status | grep cos-proxy-monitors` to be active/idle")
    action_plan.append(f"export JUJU_CONTROLLER={main_controller_name}")
    action_plan.append(f"export JUJU_MODEL={main_model_name}")
    action_plan.append("juju add-relation cos-proxy-monitors:downstream-prometheus-scrape "
                       "cos-scrape-interval-config-monitors:configurable-scrape-jobs")

    print(os.linesep.join(action_plan))


if __name__ == "__main__":
    get_ap()

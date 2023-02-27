from copy import deepcopy
import click
import difflib
import dns.resolver
import ipaddress
import json
import logging
from prettytable import PrettyTable
import requests
import sys
import urllib3

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)

class UnifiControllerSession:
    def __init__(self, controller_url: str, user: str, password: str):
        self.controller_url = controller_url
        self.session = get_unifi_login(controller_url, user, password)

    def get(self, rest_path: str) -> requests.Response:
        response = self.session.get(self.controller_url + "/" + rest_path)
        verify_rest_response(response)
        return response

    def put(self, rest_path: str, data) -> requests.Response:
        response = self.session.put(self.controller_url + "/" + rest_path, data=data)
        verify_rest_response(response)
        return response


def verify_rest_response(response: requests.Response, quit=True) -> str:
    # The default behavior is to just pass in the response and
    # the code will sys.exit with the error. To test for a specific
    # issue, set quit=False and test the response. If it's an empty string,
    # the response is valid, if not the string will return the raw error
    # from the Unifi REST request.
    if response.status_code != 200:
        logging.critical(f"Unifi Controller response error:\n{response.text}")
        if quit:
            sys.exit(response.text)
        return response.text

    auth_result = response.json()

    # valid responses are in this form
    # {
    #   "meta": {
    #     "rc": "ok"
    #   },
    #   "data": {...}
    # }
    if auth_result["meta"]["rc"] != "ok":
        logging.critical(f"Unifi Controller response error:\n{auth_result}")
        if quit:
            sys.exit(auth_result)
        return json.dumps(auth_result, indent=2)

    # Fell through so the response is valid
    return ""


def get_host_ip_dict(dns_host_file: str) -> dict:
    # Reads in the list of dns names we want to resolve.
    # The original version if this script was created to automatically
    # maintain the whitelist of Grafana Cloud Synthetic Monitoring hosts
    # but block PING from all other hosts. (Keeping your network a dark hole
    # from the outside is good manners.) Use any list you want, just one
    # host per line, blank lines and comments allowed.

    logging.info(f'Reading host list file "{dns_host_file}"')

    # Read in the file
    host_ip_list = {}
    with open(dns_host_file, "r") as fh:
        for host_line in fh:  # Read each line
            host_line = host_line.strip()  # remove whitespace to be friendly
            if len(host_line) > 0 and not host_line.startswith("#"):
                # get the DNS 'A' records for each. Currently this will add
                # all IP's returned for each host
                answer = dns.resolver.resolve(host_line, "A")
                for record in answer:
                    ip = str(record)
                    # The Unifi controller will barf if you try to push duplicate
                    # IPs into a firewall group. Meanwhile, you might have get the same
                    # IP for multiple DNS lookups. So we throw those away.
                    host_ip_list[str(record)] = host_line

    return host_ip_list


def get_unifi_login(controller_url: str, user: str, password: str) -> requests.Session:
    # We need a Session for the REST connection because, Unifi cookies
    # disable the warning on stdout for Unifi controller certs
    logging.info("Logging into Unifi Controller")
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.headers = {"Content-Type": "application/json", "Accept": "application/json"}
    session.verify = False
    payload = {"username": user, "password": password}

    # Request a login
    response = session.post(
        controller_url + "/api/login", data=json.dumps(payload), verify=False
    )

    verify_rest_response(response)
    logging.debug("Unifi Controller log in successful")
    return session


def get_firewall_group(ucs: UnifiControllerSession, group_id: str):
    logging.info("Getting Unifi Controller Firewall Group")
    r = ucs.get("api/s/default/rest/firewallgroup")
    # TODO: Verify result, exit if invalid

    unifi_result = r.json()
    for group in unifi_result["data"]:
        if group["_id"] == group_id:
            return deepcopy(group)

    # We fell through, so bomb out
    sys.exit(f"Firewall Group {group_id} not found in:\n{unifi_result}")


def build_firewall_group_update(host_ips: dict, unifi_group_old: dict):
    # fist we clone and clear out the old IP list
    new_group = deepcopy(unifi_group_old)
    new_group["group_members"] = []

    for hip in host_ips:
        new_group["group_members"].append(hip)

    return new_group


def diff_groups(old_group: dict, new_group: dict, host_ips: dict) -> dict:
    result = {"table": "", "changes": 0}
    changes = 0
    new_list = new_group["group_members"]
    old_list = old_group["group_members"]
    old_list.sort(key=ipaddress.IPv4Address)

    diff = difflib.ndiff(old_list, new_list)

    pt = PrettyTable()
    pt.field_names = ["Action", "IP Address", "Hostname (if known)"]
    pt.align["IP Address"] = "l"
    pt.align["Hostname (if known)"] = "l"
    for d in diff:
        action = ""
        match d[0]:
            case " ":
                action = "Keep"
            case "+":
                changes += 1
                action = "Add"
            case "-":
                changes += 1
                action = "Remove"

        if action != "":
            # We don't have the host list from the existing Unifi firewall group list
            # So blank if not found
            host = ""
            if d[2:] in host_ips:
                host = host_ips[d[2:]]

            pt.add_row([action, d[2:], host])

    result["table"] = pt.get_string()
    result["changes"] = changes

    return result


def update_unifi_controller(ucs: UnifiControllerSession, new_firewall_group: dir):
    logging.info("Updating Unifi Controller Firewall Group")
    r = ucs.put(
        "api/s/default/rest/firewallgroup/" + new_firewall_group["_id"],
        data=json.dumps(new_firewall_group),
    )

    verify_rest_response(r)


@click.command()
@click.option(
    "--unifihostpath",
    prompt="Unifi Controller URL path",
    help="Full URL to the Unifi Controller including protocol, host, and port (if required)."
    ' For example the default is "http://{hostname|IP}:8443". Use "https://" if SSL is enabled.',
)
@click.option(
    "--user",
    prompt="Unifi Controller user",
    help="Username for the Unifi Controller",
)
@click.option(
    "--password",
    hide_input=True,
    confirmation_prompt=True,
    prompt="Unifi Controller password",
    help="Password for the Unifi Controller",
)
@click.option(
    "--groupid",
    prompt="Firewall group ID",
    help='Unifi firewall group ID to update. To find it, log into the Unifi Controller, click "Edit"'
    " for the group to update, and copy the alphanumeric group ID from the end of the URL in the browser.",
)
@click.option(
    "--hostfile",
    type=click.Path(exists=True),
    prompt="Path to host list file",
    help="Path to the input file containing the list of hostnames for DNS lookup and update into "
    ' the Unifi Controller for the Firewall Group ID specified by "groupid"',
)
@click.option(
    "--confirm",
    default=True,
    prompt="Confirm changes before update?",
    help="Asks to confirm the update before proceeding, default=True. For silent update set to false",
)
@click.option(
    "--loglevel",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    prompt="Log level",
    help="Sets the log level",
)
def update_unifi_ip_group(
    unifihostpath: str,
    user: str,
    password: str,
    groupid: str,
    hostfile: str,
    confirm: bool,
    loglevel: str,
):
    logging.getLogger().setLevel(logging.getLevelName(loglevel))

    host_ips = get_host_ip_dict(hostfile)
    # Sort by IP address for tidiness in the Unifi Firewall UI
    host_ips = dict(
        sorted(host_ips.items(), key=lambda tup: ipaddress.IPv4Address(tup[0]))
    )

    logging.debug(f"IP's from hostname list lookups:\n{json.dumps(host_ips, indent=2)}")

    ucs = UnifiControllerSession(unifihostpath, user, password)

    old_group = get_firewall_group(ucs, groupid)

    logging.debug(
        f"Current Firewall Group on the Unifi Controller:\n{json.dumps(old_group, indent=2)}"
    )

    new_group = build_firewall_group_update(host_ips, old_group)

    logging.debug(
        f"New Firewall Group to push to the Unifi Controller:\n{json.dumps(new_group, indent=2)}"
    )

    diff_result = diff_groups(old_group, new_group, host_ips)
    logging.info(
        f'Found {diff_result["changes"]} differences between old and new Firewall Groups:\n{diff_result["table"]}'
    )

    if diff_result["changes"] > 0:

        if confirm:
            if logging.root.level > logging.INFO:
                print(
                    f'Here is the list of {diff_result["changes"]} Unifi Firewall Group changes to make:\n{diff_result["table"]}'
                )
            apply = click.prompt(
                text="Apply changes to Unifi Controller?", type=bool, default=True
            )
            if apply == False:
                logging.info("Update canceled by user")
                return

        update_unifi_controller(ucs, new_group)
        logging.info("Unifi Controller Update complete")
    else:

        logging.info("No changes found, update skipped")


if __name__ == "__main__":

    update_unifi_ip_group()

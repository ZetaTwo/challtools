import os
import re
import subprocess
import sys
import hashlib
import json
from pathlib import Path
import yaml
import docker
import requests
from .validator import ConfigValidator
from .constants import *


class CriticalException(Exception):
    pass


def process_messages(messages, verbose=False):
    """Processes a list of messages from validator.ConfigValidator.validate for printing.

    Args:
        messages (list): The list of messages returned by validator.ConfigValidator.validate

    Returns:
        dict: Dictionary with the following keys:

            ``message_strings`` (*list*)
                A list of strings containing formatted messages, with ANSI color codes. Example string: ``[CRITICAL] [A002] Schema violation``
            ``count_string`` (*str*)
                A string listing the counts of each message level, with ANSI color codes. Example: ``1 MEDIUM and 1 HIGH issue raised.``
            ``highest_level`` (*int*)
                The highest message level in the message list. Always between 0-5, where 0 is no messages and 5 is CRITICAL.
    """
    level_counts = [0, 0, 0, 0, 0]
    highest_level = 0
    message_strings = []
    for message in messages:
        level_counts[message["level"] - 1] += 1
        highest_level = max(highest_level, message["level"])

        message_string = (
            f"[{STYLED_LEVELS[message['level']-1]}] [{BOLD}{message['code']}{CLEAR}] "
        )
        if message["field"]:
            message_string += f"{message['field']}: "
        message_string += message["name"]
        if verbose:
            message_string += "\n" + message["message"]
        message_strings.append(message_string)

    level_name_counts = {i: count for i, count in enumerate(level_counts) if count}
    count_string = ""

    if level_name_counts:
        count_string += "\n"

    if not level_name_counts:
        count_string += "No"
    else:
        count_string += " and ".join(
            ", ".join(
                f"{count[1]} {STYLED_LEVELS[count[0]]}"
                for count in level_name_counts.items()
            ).rsplit(", ", 1)
        )

    count_string += f" issue{'s' if not level_name_counts or list(level_name_counts.values())[-1] > 1 else ''} raised."

    return {
        "message_strings": message_strings,
        "count_string": count_string,
        "highest_level": highest_level,
    }


def get_ctf_config_path():
    """Locates the global CTF configuration file (ctf.yml) and returns a path to it.

    Returns:
        pathlib.Path: The path to the config
        None: If there was no CTF config
    """
    p = Path(".").absolute()

    for directory in [p, *p.parents]:
        if (directory / "ctf.yml").exists():
            return directory / "ctf.yml"
        if (directory / "ctf.yaml").exists():
            return directory / "ctf.yaml"

    return None


def load_ctf_config():
    """Loads the global CTF configuration file (ctf.yml) from the current or a parent directory.

    Returns:
        dict: The config
        None: If there was no CTF config
    """
    ctfpath = get_ctf_config_path()

    if not ctfpath:
        return None

    with ctfpath.open() as f:
        raw_config = f.read()
    config = yaml.safe_load(raw_config)

    return config if config else {}


def load_config(workdir=".", search=True, cd=True):
    """Loads the challenge configuration file from the current directory, a specified directory, or optionally one of their parent directories. Optionally changes the working directory to the directory of the configuration file.

    Args:
        workdir (string): The directory to search for the configuration file from
        search (bool): If the parent directories of the starting directory should be searched for the configuration file
        cd (bool): If the working directory should be set to the directory the configuration file is found in

    Returns:
        dict: The config

    Raises:
        CriticalException: If the challenge configuration cannot be found
    """

    workdir = Path(workdir).absolute()

    for directory in [workdir, *(workdir.parents if search else [])]:
        path = Path(directory)
        if (path / "challenge.yml").exists():
            path = path / "challenge.yml"
            break
        elif (path / "challenge.yaml").exists():
            path = path / "challenge.yaml"
            break
    else:
        raise CriticalException(
            f"Could not find a challenge.yml file in this{' or a parent' if search else ''} directory."
        )

    with path.open() as f:
        raw_config = f.read()
    config = yaml.safe_load(raw_config)

    if cd:
        path.parent.cwd()

    return config


def get_valid_config(workdir=None, search=True, cd=True):
    """Loads the challenge configuration file from the current directory and makes sure its valid.

    Args:
        workdir (string): The directory to search for the configuration file from
        search (bool): If the parent directories of the starting directory should be searched for the configuration file
        cd (bool): If the working directory should be set to the directory the configuration file is found in

    Returns:
        dict: The normalized config

    Raises:
        CriticalException: If there are critical validation errors
    """
    config = load_config(
        search=search, cd=cd, **{"workdir": workdir} if workdir else {}
    )

    validator = ConfigValidator(config)
    messages = validator.validate()[1]
    highest_level = process_messages(messages)["highest_level"]

    if highest_level == 5:
        print(
            "\n".join(
                process_messages([m for m in messages if m["level"] == 5])[
                    "message_strings"
                ]
            )
        )
        print()
        raise CriticalException(
            "There are critical config validation errors. Please fix them before continuing."
        )
    elif highest_level == 4:
        print(
            "\n".join(
                process_messages([m for m in messages if m["level"] == 4])[
                    "message_strings"
                ]
            )
        )
        print(
            f"\n{HIGH}There are config validation issues of high severity. You probably want to fix them.{CLEAR}"
        )

    return validator.normalized_config


def discover_challenges():
    """Discovers all challenges at the same level as or in a subdirectory below the CTF configuration file.

    Returns:
        list: A list of pathlib.Path objects to all found challenge configurations
        None: If there was no CTF config
    """
    root = get_ctf_config_path().parent

    if not root:
        return None

    def checkdir(d):
        if (d / "challenge.yml").exists():
            return [d / "challenge.yml"]
        if (d / "challenge.yaml").exists():
            return [d / "challenge.yaml"]
        results = []
        for subd in [f for f in d.iterdir() if f.is_dir()]:
            results += checkdir(subd)
        return results

    return checkdir(root)


def get_docker_client():
    """Gets an authenticated docker client.

    Returns:
        docker.client.DockerClient: The docker client

    Raises:
        CriticalException: If the client cannot be created
    """
    try:
        client = docker.from_env()
        client.images.list()
    except (requests.exceptions.ConnectionError, docker.errors.DockerException):
        raise CriticalException(
            "challtools needs root permissions to run docker (run with sudo)"
        )

    return client


def get_first_text_flag(config):
    """Creates a valid flag with the flag format using the flag format and the first text flag, if it exists.

    Args:
        config (dict): The normalized challenge config

    Returns:
        string: A valid flag. May be an empty string, in which case it's probably not a valid flag. This happens if there is no text type flag.
    """

    text_flag = None
    for flag in config["flags"]:
        if flag["type"] == "text":
            text_flag = flag["flag"]
            break
    else:
        return ""

    if not config["flag_format_prefix"]:
        return text_flag

    return config["flag_format_prefix"] + text_flag + config["flag_format_suffix"]


def create_docker_name(title, container_name=None, chall_id=None):
    """Converts challenge information into a most likely unique and valid docker tag name.

    Args:
        title (string): The challenge title
        container_name (string): The name of the container whose name this is
        chall_id (string): The challenge_id

    Returns:
        string: A valid docker tag name no longer than 66 characters.
    """
    digest = hashlib.md5(
        (title + "|" + (container_name or "") + "|" + (chall_id or "")).encode()
    ).hexdigest()

    title = title.replace(" ", "_")
    title = re.sub(r"[^A-Za-z0-9_.-]", "", title)
    title = title.lstrip("_.-")
    title = title.lower()

    if container_name:
        container_name = container_name.replace(" ", "_")
        container_name = re.sub(r"[^A-Za-z0-9_.-]", "", container_name)
        container_name = container_name.lstrip("_.-")
        container_name = container_name.lower()

        return "_".join([title[:32], container_name[:16], digest[:16]])

    return "_".join([title[:32], digest[:16]])


def format_user_service(config, service_type, **kwargs):
    """Formats a string displayed to the user based on the service type and a substitution context (``user_display`` in the OpenChallSpec).

    Args:
        config (dict): The normalized challenge config
        service_type (string): The service type of the service to format
        **kwargs (string): Any substitution context. ``host`` and ``port`` must be provided for the ``tcp`` service type, and ``url`` must be provided for the ``website`` service type however this is not validated.

    Returns:
        string: A formatted string ready to be presented to a CTF player
    """
    service_types = [
        {"type": "website", "user_display": "{url}"},
        {"type": "tcp", "user_display": "nc {host} {port}"},
    ] + config["custom_service_types"]
    type_candidate = [i for i in service_types if i["type"] == service_type]

    if not type_candidate:
        raise ValueError(f"Unknown service type {service_type}")
    string = type_candidate[0]["user_display"]

    for name, value in kwargs.items():
        string = string.replace("{" + name + "}", value)

    return string


def validate_solution_output(config, output):
    """validates a flag outputted by a solver by stripping the whitespace and validating the flag.

    Args:
        config (dict): The normalized challenge config
        output (string): The output from the Solver

    Returns:
        boolean: If the flag was valid
    """
    return validate_flag(config, output.strip())


def validate_flag(config, submitted_flag):
    """validates a flag against the flags in the challenge config.

    Args:
        config (dict): The normalized challenge config
        flag (string): the flag to validate

    Returns:
        boolean: If the flag was valid
    """
    if config["flag_format_prefix"]:
        if not submitted_flag.startswith(
            config["flag_format_prefix"]
        ) or not submitted_flag.endswith(config["flag_format_suffix"]):
            return False
        submitted_flag = submitted_flag[
            len(config["flag_format_prefix"]) : -len(config["flag_format_suffix"])
        ]

    for flag in config["flags"]:
        if flag["type"] == "text":
            if submitted_flag == flag["flag"]:
                return True

        if flag["type"] == "regex":
            if re.search(flag["flag"], submitted_flag):
                return True

    return False


def build_image(image, tag, client):
    """Build a docker image given the image (as a path to a folder, if archive it will load it), the tag and the docker client.

    Args:
        image (string): The image as a path to a folder to build or as a path to an archive to import. if neither, the function won't do anything
        tag (string): The tag name to tag the image as
        client (docker.client.DockerClient): The docker client to use for building

    Raises:
        CriticalException: If the build fails
    """
    imagepath = Path(image)
    if imagepath.is_dir():
        print(
            f'{BOLD}Interpreting "{image}" as an image build directory\nBuilding image...{CLEAR}'
        )
        try:
            stream = client.api.build(
                path=str(imagepath),
                tag=tag,
                rm=True,
            )

            for chunk in stream:
                decoded = json.loads(chunk)
                # TODO process progress bars for pulling
                if "error" in decoded:
                    raise CriticalException(decoded["error"])
                if "stream" in decoded:
                    print(decoded["stream"], end="")

        except docker.errors.APIError as e:
            raise CriticalException(e.explanation)

    elif imagepath.is_file():
        print(f'{BOLD}Interpreting "{image}" as an image archive{CLEAR}')
        print(f"{BOLD}Importing image...{CLEAR}")
        raise NotImplementedError  # TODO
    else:
        print(
            f'{BOLD}Interpreting "{image}" as an existing image, nothing to build{CLEAR}'
        )


def build_chall(config):
    """Builds a challenge including running the build script and building service and solution docker images. Expects to be run from the root directory of the challenge.

    Args:
        config (dict): The normalized challenge config

    Returns:
        bool: False if there was nothing to do, True if it ran the build script or built a container

    Raises:
        CriticalException: If the build fails
    """
    did_something = False

    if config["deployment"]:
        if config["deployment"]["type"] != "docker":
            raise CriticalException(
                'challtools only supports the "docker" deployment type'
            )

        client = get_docker_client()

    if "build_script" in config["custom"] or config["deployment"]:
        flag = get_first_text_flag(config)
        if flag:
            print(f"{BOLD}Flag: {flag}{CLEAR}")

    if "build_script" in config["custom"]:
        print(f"{BOLD}Running build script...{CLEAR}")

        did_something = True

        p = subprocess.Popen(
            [Path(config["custom"]["build_script"]).absolute(), flag],
            stdout=sys.stdout,
            stderr=sys.stdout,
        )
        p.wait()

        if p.returncode != 0:
            raise CriticalException(f"Build script exited with code {p.returncode}")

    if config["deployment"]:
        did_something = True
        for container_name, container in config["deployment"]["containers"].items():
            print(f"{BOLD}Processing container {container_name}...{CLEAR}")
            build_image(
                container["image"],
                create_docker_name(
                    config["title"],
                    container_name=container_name,
                    chall_id=config["challenge_id"],
                ),
                client,
            )

        network_list = [network.name for network in client.networks.list()]
        for network_name in config["deployment"]["networks"]:
            if network_name not in network_list:
                print(f"{BOLD}Creating network {network_name}...{CLEAR}")
                client.networks.create(
                    network_name
                )  # TODO make network names not collide between challenges, add id hash maybe

        volume_list = [volume.name for volume in client.volumes.list()]
        for volume_name in config["deployment"]["volumes"]:
            if volume_name not in volume_list:
                print(f"{BOLD}Creating volume {volume_name}...{CLEAR}")
                client.volumes.create(
                    volume_name
                )  # TODO make volume names not collide between challenges, add id hash maybe

    if config["solution_image"]:
        did_something = True
        print(f"{BOLD}Processing solution image...{CLEAR}")
        build_image(
            config["solution_image"],
            "sol_"
            + create_docker_name(config["title"], chall_id=config["challenge_id"]),
            client,
        )

    return did_something


def start_chall(config):
    """Starts all docker containers for this challenge.

    Args:
        config (dict): The normalized challenge config

    Returns:
        tuple: The first element is a list of all started containers as docker.models.containers.Container instances. The second element is a list of formatted service strings for displaying to users.

    Raises:
        CriticalException: If the start fails
    """

    if not config["deployment"] or not config["deployment"]["containers"]:
        return [], []

    if config["deployment"]["type"] != "docker":
        raise CriticalException('challtools only supports the "docker" deployment type')

    client = get_docker_client()
    tag_list = [
        tag.split(":")[0]
        for img in client.images.list()
        for tag in img.attrs["RepoTags"]
    ]

    for container_name, container in config["deployment"]["containers"].items():
        tag = create_docker_name(
            config["title"],
            container_name=container_name,
            chall_id=config["challenge_id"],
        )  # TODO check that the container hasn't already been started

        if tag not in tag_list:
            raise CriticalException(
                f'Cannot find image "{tag}". Make sure you have built the required docker images using "challtools build" before attempting to start them.'
            )

    # TODO test that network and volume detection works
    network_list = [network.name for network in client.networks.list()]
    for network_name in config["deployment"]["networks"]:
        if network_name not in network_list:
            raise CriticalException(
                f'Cannot find network "{network_name}". Make sure you have created the required docker networks using "challtools build" before attempting to use them.'
            )

    volume_list = [volume.name for volume in client.volumes.list()]
    for volume_name in config["deployment"]["volumes"]:
        if volume_name not in volume_list:
            raise CriticalException(
                f'Cannot find volume "{volume_name}". Make sure you have created the required docker volumes using "challtools build" before attempting to use them.'
            )

    containers = []
    service_strings = []
    available_port = (
        50000  # TODO add some support for running challenges at the same time
    )

    for container_name, container_config in config["deployment"]["containers"].items():
        tag = create_docker_name(
            config["title"],
            container_name=container_name,
            chall_id=config["challenge_id"],
        )

        ports = {}
        for service in container_config.get("services", []):
            if "external_port" not in service:
                service["external_port"] = available_port
                available_port += 1
            ports[service["internal_port"]] = service["external_port"]

            service_strings.append(
                format_user_service(
                    config,
                    service["type"],
                    host="127.0.0.1",
                    port=str(service["external_port"]),
                    url=f"http://127.0.0.1:{service['external_port']}",
                )
            )

        for extra in container_config.get("extra_exposed_ports", []):
            ports[extra["internal_port"]] = extra["external_port"]

        container = client.containers.create(
            tag,
            ports=ports,
            detach=True,
            auto_remove=True,
            environment={"TEST": "true"},
            # TODO volumes
        )

        for network, network_containers in config["deployment"]["networks"].items():
            if container_name in network_containers:
                client.networks.get(network).connect(container)

        container.start()
        print(f"{BOLD}Started container {container_name}{CLEAR}")

        containers.append(container)

    return containers, service_strings


def start_solution(config):
    """Starts a solution container for this challenge.

    Args:
        config (dict): The normalized challenge config

    Returns:
        docker.models.containers.Container: The started docker container

    Raises:
        CriticalException: If the solution cannot be started correctly
    """

    if not config["solution_image"]:
        return None

    client = get_docker_client()
    tag_list = [
        tag.split(":")[0]
        for img in client.images.list()
        for tag in img.attrs["RepoTags"]
    ]
    solution_tag = "sol_" + create_docker_name(
        config["title"], chall_id=config["challenge_id"]
    )

    if solution_tag not in tag_list:
        raise CriticalException(
            f'Cannot find solution image "{solution_tag}". Make sure you have built the required solution docker image using "challtools build" before attempting to start it.'
        )

    service_strings = []
    available_port = 50000

    for predefined_service in config["predefined_services"]:
        service_strings.append(
            format_user_service(config, service["type"], **predefined_service)
        )

    for container_name, container_config in config["deployment"]["containers"].items():
        for service in container_config.get(
            "services", []
        ):  # FIXME validator breaks spec because .get is required, this should always default to an empty array
            if "external_port" not in service:
                service["external_port"] = available_port
                available_port += 1
            service_strings.append(
                format_user_service(
                    config,
                    service["type"],
                    host="127.0.0.1",
                    port=str(service["external_port"]),
                    url=f"http://127.0.0.1:{service['external_port']}",
                )
            )

    container = client.containers.create(
        solution_tag,
        detach=True,
        network="host",
        environment={"TEST": "true"},
        command=service_strings,
    )
    container.start()

    return container

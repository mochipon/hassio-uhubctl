import argparse
import datetime
import json
import logging
import os
import re
import subprocess

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
logger.addHandler(handler)
handler.setFormatter(logging.Formatter("[%(asctime)s] [%(funcName)s] %(message)s"))
logger.propagate = False


def run_in_shell(command, timeout=10):
    try:
        logger.debug("Command kicked: {command}".format(command=command))
        ret = subprocess.run(
            command, shell=True, timeout=timeout, stdout=subprocess.PIPE, text=True
        )
        logger.debug(
            "Command exited with exit code {exit_code}".format(exit_code=ret.returncode)
        )
        return ret
    except Exception:
        logger.exception("Fatal error in running a command")
        raise


class USBHUB:
    def __init__(self, location, vid, pid, usbversion, nports, ports):
        self._location = location
        self._vid = vid
        self._pid = pid
        self._usbversion = usbversion
        self._nports = nports
        self._ports = ports

    def add_port(self, number, status):
        self._ports.append(USBPORT(self.location, number, status))

    @property
    def location(self):
        return self._location

    @property
    def vid(self):
        return self._vid

    @property
    def pid(self):
        return self._pid

    @property
    def usbversion(self):
        return self._usbversion

    @property
    def nports(self):
        return self._nports


class USBPORT:
    def __init__(self, hub_location, number, status):
        self._hub_location = hub_location
        self._number = number
        self._enabled = status

    def on(self):
        self._enabled = True

    def off(self):
        self._enabled = False

    @property
    def hub_location(self):
        return self._hub_location

    @property
    def number(self):
        return self._number

    @property
    def enabled(self):
        return self._enabled


class UHUBCTL:
    def _parser(self, stdout, action=False):
        ret = []

        result = stdout.strip().split("\n")

        try:
            lineidxs_hubheader = [
                index for index, line in enumerate(result) if "status for hub" in line
            ]
            if not len(lineidxs_hubheader) > 0:
                raise ValueError
        except ValueError:
            logger.error("Failed to find any smart hubs")
            return False

        for lineidx_hubheader in lineidxs_hubheader:
            parsed_line = re.search(
                r"status for hub ([0-9-]+) \[([0-9a-f]{4}):([0-9a-f]{4}).*USB (\d)\.\d{2}, (\d+) ports, ppps",
                result[lineidx_hubheader],
            )
            if parsed_line is None:
                continue

            hub = USBHUB(
                location=parsed_line.group(1),
                vid=int(parsed_line.group(2), 16),
                pid=int(parsed_line.group(3), 16),
                usbversion=int(parsed_line.group(4)),
                nports=int(parsed_line.group(5)),
                ports=[],
            )

            lineidx_port_start = lineidx_hubheader + 1
            lineidx_port_end = (
                lineidx_port_start + hub.nports
                if not action
                else lineidx_port_start + 1
            )

            for lineidx in range(lineidx_port_start, lineidx_port_end):
                # Port Information
                parsed_line = re.search(r"Port (\d+): (\d{4})", result[lineidx])
                if parsed_line is None:
                    continue
                port_number = int(parsed_line.group(1))
                port_status_bit = int(parsed_line.group(2), 16)
                logger.debug(
                    "Hub {location} Port {port_number} = {port_status_bit:#06x}".format(
                        location=hub.location,
                        port_number=port_number,
                        port_status_bit=port_status_bit,
                    )
                )

                if hub.usbversion == 3:
                    # USB 3.0 spec Table 10-10
                    # USB_SS_PORT_STAT_POWER = 0x0200
                    POWER_ON_BIT = 0x0200
                else:
                    # USB 2.0 spec Table 11-21
                    # USB_PORT_STAT_POWER = 0x0100
                    POWER_ON_BIT = 0x0100

                if port_status_bit & POWER_ON_BIT:
                    port_status = True
                else:
                    port_status = False

                hub.add_port(port_number, port_status)

            ret.append(hub)

        return ret

    def fetch_allinfo(self):
        try:
            logger.debug("Fetch current status for all smart hubs")
            ret = run_in_shell("uhubctl")
            stdout = ret.stdout

            return self._parser(stdout)
        except:
            logger.exception("Failed to fetch current status")
            return None

    def do_action(self, port, action):
        try:
            action = action.lower()
            if action not in ["on", "off"]:
                raise ValueError
        except ValueError:
            logger.error(
                "Illegal action to the port: action={action}".format(action=action)
            )
            return False

        logger.debug(
            "Send command to the port: hub={location}, port={port}, action={action}".format(
                location=port.hub_location, port=port.number, action=action
            )
        )

        try:
            ret = run_in_shell(
                "uhubctl -l {location} -p {port} -a {action} -r 100".format(
                    location=port.hub_location, port=port.number, action=action
                )
            )
            stdout = ret.stdout

            # _parser returns [Current status, New status]
            newstatus_hub = self._parser(stdout, action=True)[-1]
            newstatus_port = newstatus_hub._ports[0]

            if newstatus_port.enabled:
                port.on()
            else:
                port.off()

            return True
        except:
            logger.exception(
                "Failed to change port status: hub={location}, port={port}, action={action}".format(
                    location=port.hub_location, port=port.number, action=action
                )
            )
            return False


class USBHUB_MQTT_Error(Exception):
    pass


class USBHUB_MQTT:
    def __init__(self, opt_file):
        with opt_file:
            self._cfg = json.load(opt_file)
            self._usbhubs = []
            self._will = (self._cfg["AVAILABILITY_TOPIC"], "Offline", 1, True)

    def make_json_portstatus(self, usbhub):
        ret = {
            "Time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Location": usbhub.location,
            "Vid": usbhub.vid,
            "Pid": usbhub.pid,
            "USBVersion": usbhub.usbversion,
        }

        for port in usbhub._ports:
            idx = "POWER{number}".format(number=port.number)
            ret[idx] = "ON" if port.enabled else "OFF"

        return json.dumps(ret)

    def send_mqtt_hubstatus(self, client, usbhub=None):
        usbhubs = self._usbhubs if usbhub is None else [usbhub]

        for usbhub in usbhubs:
            topic = "{prefix}/HUB{location}/STATE".format(
                prefix=self._cfg["STATUS_TOPIC"], location=usbhub.location
            )
            payload = self.make_json_portstatus(usbhub)

            logger.debug(
                "MQTT Publish current status: topic={topic}, payload={payload}".format(
                    topic=topic, payload=payload
                )
            )

            client.publish(
                topic=topic,
                payload=payload,
                qos=1,
                retain=True,
            )

    def on_mqtt_connect(self, client, userdata, flags, rc):
        if rc != 0:
            raise USBHUB_MQTT_Error(
                "Error while connecting to the MQTT broker. Reason code: {}".format(
                    str(rc)
                )
            )
        else:
            logger.info("MQTT Connected successfully")

        result, mid = client.subscribe(self._cfg["COMMAND_TOPIC"] + "/#", 1)
        logger.info(
            "MQTT Subscribe: topic={topic}, result={result}".format(
                topic=self._cfg["COMMAND_TOPIC"] + "/#",
                result="Success" if result == mqtt.MQTT_ERR_SUCCESS else "Failed",
            )
        )

        self._usbhubs = UHUBCTL().fetch_allinfo()

        self.send_mqtt_hubstatus(client)
        client.publish(
            topic=self._cfg["AVAILABILITY_TOPIC"], payload="Online", qos=1, retain=True
        )

    def on_mqtt_ctrl_message(self, client, userdata, message):
        logger.info(
            "Received a control message: topic={topic}, payload={payload}".format(
                topic=message.topic, payload=message.payload.decode()
            )
        )

        # Topic will be "hoge/usbhub/HUB1-3/POWER1
        parsed_topic = message.topic.split("/")
        try:
            command = parsed_topic[-1]
            hub_name = parsed_topic[-2]
            hub_location = re.search(r"HUB([0-9-]+)", hub_name).group(1)
        except IndexError:
            logger.error("Failed to parse the topic string")
            return False

        parsed_command = re.search(r"([A-Z]+)(\d+)", command)

        if parsed_command.group(1) == "POWER":
            try:
                port_number = int(parsed_command.group(2))
                hub = [hub for hub in self._usbhubs if hub.location == hub_location][0]
                port = [port for port in hub._ports if port.number == port_number][0]
            except IndexError:
                logger.error("Illigal action request to unknown hub / port")
                return False

            try:
                action = message.payload.decode()
                UHUBCTL().do_action(port, action)
            except:
                logger.exception("Failed to execute an action")

        self.send_mqtt_hubstatus(client, hub)

    def loop_forever(self):
        try:
            mqtt_hostname = os.environ["MQTT_HOST"]
            mqtt_port = int(os.environ["MQTT_PORT"])
            mqtt_username = os.environ["MQTT_USERNAME"]
            mqtt_password = os.environ["MQTT_PASSWORD"]
            logger.debug(
                "MQTT Server: mqtt://{username}:<secret>@{host}:{port}".format(
                    username=mqtt_username,
                    host=mqtt_hostname,
                    port=mqtt_port,
                )
            )
        except KeyError:
            logger.exception("Failed to fetch local MQTT configurations")
            return False

        mqc = mqtt.Client()
        mqc.on_connect = self.on_mqtt_connect
        mqc.message_callback_add(
            self._cfg["COMMAND_TOPIC"] + "/#", self.on_mqtt_ctrl_message
        )
        mqc.username_pw_set(mqtt_username, mqtt_password)
        mqc.will_set(*self._will)

        mqc.connect(mqtt_hostname, mqtt_port)

        mqc.loop_forever()


if __name__ == "__main__":
    argp = argparse.ArgumentParser(description="MQTT - uhubctl bridge")
    argp.add_argument(
        "-c",
        "--config",
        type=argparse.FileType(),
        default="/data/options.json",
        help="User configuration file genereted by Home Assistant",
    )

    log_levels = ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    log_levels = log_levels + list(map(lambda w: w.lower(), log_levels))
    argp.add_argument("--log", choices=log_levels, default="INFO", help="Logging level")

    args = vars(argp.parse_args())

    logger.setLevel(level=args["log"].upper())
    handler.setLevel(level=args["log"].upper())

    usbhub_mqtt = USBHUB_MQTT(args["config"])
    usbhub_mqtt.loop_forever()

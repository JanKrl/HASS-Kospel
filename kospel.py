"""AppDaemon script for Home Assistant
Integration with Kospel electric heaters
"""
from datetime import time
from enum import Enum
import re
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchAttributeException,
    TimeoutException,
    ElementNotInteractableException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import hass


class Kospel(hass.Hass):
    """Handling addon"""

    SENSORS = {
        "temp_room": {
            "device_class": "temperature",
            "friendly_name": "Room temperature",
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer",
        },
        "temp_outside": {
            "device_class": "temperature",
            "friendly_name": "Outside temperature",
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer",
        },
        "temp_boil": {
            "device_class": "temperature",
            "friendly_name": "Tap water temperature",
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer",
        },
        "power": {
            "device_class": "power",
            "friendly_name": "Current power",
            "unit_of_measurement": "kW",
            "icon": "mdi:lightning-bolt-outline",
        },
    }

    STATUSES = {
        "radiator": {
            "device_class": "running",
            "friendly_name": "Radiators heating",
            "initial_state": "off",
            "icon": "mdi:radiator",
        },
        "tap": {
            "device_class": "running",
            "friendly_name": "Tap water heating",
            "initial_state": "off",
            "icon": "mdi:water-pump",
        },
        "pump": {
            "device_class": "running",
            "friendly_name": "Heating pump",
            "initial_state": "off",
            "icon": "mdi:pump",
        },
        "error": {
            "device_class": "binary_sensor",
            "friendly_name": "Error",
            "initial_state": "off",
            "icon": "mdi:alert-circle",
        },
    }

    SETTINGS = {
        "temp_prog": {
            "device_class": "temperature",
            "friendly_name": "Room temperature setting",
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer",
        },
        "temp_zas_nas": {
            "device_class": "temperature",
            "friendly_name": "Tap water temperature setting",
            "unit_of_measurement": "°C",
            "icon": "mdi:thermometer",
        },
    }

    def initialize(self):
        self.name = "kospel"
        self.web_scrap = WebScrap(
            self.args["url"],
            self.args["username"],
            self.args["password"],
            self.args["exec_path"],
            self.log,
        )
        self.raw_status = None
        self.raw_params = None

        self.log("Initialized")
        self.run_minutely(self.read_data, time(0, 0, 31))

    def terminate(self):
        """App is reloading. Stop the driver"""
        try:
            self.web_scrap.stop()
        finally:
            self.addon_state("off")

    def read_data(self, kwargs=None):
        """Fetch update from Kospel Panel"""
        self.log("Reading data")
        try:
            statuses, params, settings = self.web_scrap.run()
        except (ConnectionError, ReferenceError) as e:
            self.log(e)
            self.addon_state("off")
        except Exception as e:
            self.log(f"Uncaught exception {e}. Quitting webdriver.")
            self.addon_state("off")
            self.web_scrap.stop()
        else:
            self.process_params(params)
            self.process_statuses(statuses)
            self.process_settings(settings)
            self.addon_state("on")

    def sensor_state(self, sensor, value, attributes=None):
        """Update a sensor"""
        sensor_name = f"sensor.{self.name}_{sensor}"

        if sensor in self.SENSORS:
            attributes_update = self.SENSORS.get(sensor)
        elif sensor in self.STATUSES:
            attributes_update = self.STATUSES.get(sensor)
        elif sensor in self.SETTINGS:
            attributes_update = self.SETTINGS.get(sensor)
        else:
            attributes_update = {}

        if attributes:
            attributes_update.update(attributes)

        # self.log(f"Updating sensor {sensor_name}: {value}")
        self.set_state(
            sensor_name,
            state=value,
            attributes=attributes_update,
        )

    def reset(self):
        """Sets all statuses to default state"""
        for item in [*self.SENSORS, *self.STATUSES, *self.SETTINGS]:
            self.sensor_state(item, "Unavailable")

    def addon_state(self, state):
        """Update state of the addon"""
        if state == "off":
            # Most probably there was some web scrap error
            # Set this to go through entire login process again
            self.web_scrap.stop()
            self.reset()

        self.set_state(f"{self.name}.state", state=state)

    def process_params(self, params):
        """Processes raw data from web scraper
        Look for values expected according to sensor definition

        Args:
            params (dict): Parameters values
        """
        for item in self.SENSORS:
            # Look for the value
            readout = params.get(f"params_{item}")
            if not readout:
                self.log(f"Parameter {item} not found!")
                continue

            # Extract value and unit
            value, unit = readout.split(" ")
            if not value.replace(".", "").replace("-", "").isnumeric():
                # isnumeric() looks for digits
                # it fails on detecting comma or minus sign
                self.log(f"Non-numeric value ({value}) for {item}!")
                continue
            value = float(value)

            # Update sensor
            self.sensor_state(item, value, attributes={"unit_of_measurement": unit})

    def process_settings(self, settings):
        for item in self.SETTINGS:
            readout = settings.get(item)
            if not readout:
                self.log(f"Setting {item} not found!")
                continue

            readout = readout.replace("°", "")  # Remove degree sign
            if not readout.replace(".", "").replace("-", "").isnumeric():
                # isnumeric() looks for digits
                # it fails on detecting comma or minus sign
                self.log(f"Non-numeric value ({readout}) for {item}!")
                continue
            value = float(readout)

            # Update sensor
            self.sensor_state(item, value)

    def process_statuses(self, statuses):
        """Process value of status icons

        Args:
            statuses (dics): statuses color codes
        """
        for item in self.STATUSES:
            rgb_color = statuses.get(item)
            if not rgb_color:
                self.log(f"Status {item} not found!")
                continue

            if rgb_color == StateColors.GREEN:
                description = "standby"
            elif rgb_color == StateColors.RED:
                description = "active"
            elif rgb_color == StateColors.GRAY:
                description = "on"
            elif rgb_color == StateColors.WHITE:
                description = "off"
            elif rgb_color == StateColors.BLACK:
                description = "unknown"
            else:
                self.log(f"Unknown status color {item} => {rgb_color}")
                description = "unknown"

            self.sensor_state(
                item, description, attributes={"rgb_color": self.get_rgb(rgb_color)}
            )

    @staticmethod
    def get_rgb(rgb_string):
        """Extracts R G B colors from string
        Returns:
            tuple: (red, blue, green)
        """
        color_groups = re.search(
            r"^rgb\(([\d]{1,3}), ([\d]{1,3}), ([\d]{1,3})\)$", rgb_string
        )
        if color_groups:
            return color_groups.groups()


class StateColors(str, Enum):
    """Status colors used in UI"""

    GREEN = "rgb(0, 170, 0)"
    RED = "rgb(255, 0, 0)"
    WHITE = "rgb(233, 233, 233)"
    GRAY = "rgb(133, 133, 133)"
    BLACK = "rgb(0, 0, 0)"


class WebScrap:
    """Scrapping data from Kospel Home Assistant module"""

    STATUS = [
        "radiator",  # Status ogrzewania
        "tap",  # Status grzania CWU
        "clock",  # Times
        "pump",  # Pompa
        "error",  # Blad
        "suitcase",  # Tryb urlopowy
    ]

    SETTINGS = [
        "temp_prog",  # Temperatura pokojowa zaprogramowana
        "temp_zas_nas",  # Temperatura CWU zaprogramowana
    ]

    PARAMS = [
        "params_temp_in",  # Temperatura wlotowa
        "params_temp_out",  # Temperatura wylotowa
        "params_temp_factor",  # Nastawa czynnika
        "params_temp_room",  # Temperatura pokojowa
        "params_temp_outside",  # Temperatura zewnetrzna
        "params_temp_boil",  # Temperatura zasobnika
        "params_power",  # Moc zalaczona
        "params_preasure",  # Cisnienie
        "params_flow",  # Przeplyw
    ]

    def __init__(self, url, username, password, exec_path, log_function=None):
        """Initialize selenium driver with all required options
        Set which parameters should be read
        """
        self.url = url
        self.username = username
        self.password = password
        self.logged_in = False

        if log_function:
            self.log = log_function
        else:
            # empty function with one argument
            self.log = lambda x: None

        self._build_driver(exec_path)

    def stop(self):
        """Closes web driver"""
        try:
            self.driver.quit()
        finally:
            self.logged_in = False

    def run(self):
        """Collect data from web portal

        returns (dict, dict): dictionary with statuses and parameters
        """
        # Check if current page is already "main"
        home_element = self._find_element(by=By.ID, value="path7")
        if home_element:
            self.logged_in = True
        else:
            self.logged_in = False
            self._login_and_navigate()

        if not self.logged_in:
            raise PermissionError("Not logged in!")

        # Reading data
        result_status = self._read_status()
        result_settings = self._read_settings()
        self._goto_params_page()
        result_params = self._read_params()

        # Navigate back to main page
        self._back_to_main()

        return result_status, result_params, result_settings

    def _build_driver(self, exec_path):
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-dev-shm-usage")

        service = Service(executable_path=exec_path)
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

    def _login_and_navigate(self):
        """Navigates from login page to main apge"""
        self._login()
        self._goto_device()
        self._goto_module()
        self._await_main_page()
        self.logged_in = True

    def _get_page(self, url):
        """Loads URL content and handles potential errors"""
        try:
            self.driver.get(url)
        except Exception as error:
            self.stop()
            raise ConnectionError(f"Unable to reach URL: {url}") from error

    def _login(self):
        """Service login page"""
        self.log("Logging in")
        # Read login page into the driver
        self._get_page(self.url)

        login = self._wait_for_element(by=By.ID, value="login")
        password = self._wait_for_element(by=By.ID, value="pass")

        login.send_keys(self.username)
        password.send_keys(self.password)

        zaloguj = self._find_element(
            by=By.LINK_TEXT, value="zaloguj", required=True, interactible=True
        )
        zaloguj.click()

    def _goto_device(self):
        """Select a device (after login)"""
        self._wait_for_element(by=By.CLASS_NAME, value="ui-body")

        # Page contains list with available devices
        # I assume here that there is only one device
        elements = self._find_elements(by=By.TAG_NAME, value="li", required=True)
        elements[0].click()

    def _goto_module(self):
        """Selects a module in the device.
        We're interested in management module
        """
        self._wait_for_element(by=By.ID, value="start")
        self.driver.execute_script(
            "loadModule('101','19');"
        )  # TODO check if this is OK

    def _await_main_page(self):
        # Path7 is "home image"
        self._wait_for_element(by=By.ID, value="path7")

    def _read_status(self):
        """Status of services is encoded in icons' colors"""
        status = {}
        for icon in self.STATUS:
            element = self._find_element(by=By.ID, value=f"{icon}_", required=True)
            try:
                read = element.value_of_css_property("fill")
            except NoSuchAttributeException:
                read = "rgb(0, 0, 0)"

            status[icon] = read

        return status

    def _read_settings(self):
        settings = {}
        for setting in self.SETTINGS:
            element = self._find_element(by=By.ID, value=setting)
            if element:
                settings[setting] = element.text

        return settings

    def _goto_params_page(self):
        """Parameters table is loaded into DOM at the beginning
        But the values get populated only when it's opened by the user
        """
        params_icon = self._find_element(
            by=By.ID, value="parameters_lbl_", required=True, interactible=True
        )
        params_icon.click()

        # Wait for values to be filled in
        try:
            WebDriverWait(self.driver, timeout=10, poll_frequency=1).until(
                EC.visibility_of_element_located((By.ID, "params_temp_in"))
            )
        except TimeoutException as err:
            raise ConnectionError("Timeout when opening params page") from err

    def _read_params(self):
        """Read values from parameters page"""

        params = {}
        for param in self.PARAMS:
            element = self._find_element(by=By.ID, value=param)
            params[param] = element.text if element else "---"

        return params

    def _back_to_main(self):
        """Click on "back" button and go to main page"""
        back_button = self._find_element(
            by=By.XPATH,
            value='//*[@id="params"]/div[1]/a[2]',
            required=True,
            interactible=True,
        )
        back_button.click()

        self._await_main_page()

    def _wait_for_element(self, by, value, timeout=5):
        """Waits for an element to be available and gets its value"""
        try:
            element = WebDriverWait(
                self.driver, timeout=timeout, poll_frequency=0.5
            ).until(EC.presence_of_element_located((by, value)))
        except TimeoutException as err:
            self.stop()
            raise ReferenceError(f"Timeout on waiting for ({by, value})") from err

        return element

    def _find_element(self, by, value, required=False, interactible=False):
        """Gets element from the DOM

        Args:
            locator (tuple): Tuple of selenium selector type and it's value
            required (bool, optional): Raise exception when not found.
            Defaults to False.
        """
        try:
            element = self.driver.find_element(by, value)
        except NoSuchElementException as error:
            element = None
            if required:
                self.stop()
                raise ReferenceError(
                    f"Required element {by, value} not found"
                ) from error
        except ElementNotInteractableException as error:
            element = None
            if interactible:
                self.stop()
                raise ReferenceError(f"Element {by, value} not interactible") from error
        return element

    def _find_elements(self, by, value, required=False):
        try:
            elements = self.driver.find_elements(by, value)
        except NoSuchElementException as error:
            elements = None
            if required:
                self.stop()
                raise ReferenceError(
                    f"Required elements {by, value} not found"
                ) from error

        if required and not elements:
            self.stop()
            raise ReferenceError(f"Required elements {by, value} not found")
        return elements


if __name__ == "__main__":
    addon = Kospel()

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
    InvalidSessionIdException,
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
        if not self.web_scrap.logged_in:
            self.log("Authenticating at web service")
        try:
            statuses, params, settings = self.web_scrap.run()
        except ConnectionError as e:
            self.log(e)
            self.addon_state("off")
            self.initialize()
            return
        except Exception as e:
            self.log(f"Uncaught exception {e}. Re-initializing the plugin.")
            self.addon_state("off")
            self.initialize()
            return

        self.log("Processing results")
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

        self.log(f"Updating sensor {sensor_name}: {value}")
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

    def __init__(self, url, username, password, exec_path):
        """Initialize selenium driver with all required options
        Set which parameters should be read
        """
        self.url = url
        self.username = username
        self.password = password
        self.logged_in = False

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-dev-shm-usage")
        # chrome_options.add_argument("--user-data-dir=chrome-data")
        # chrome_options.add_argument("user-data-dir=chrome-data")
        # chrome_options.add_experimental_option("detach", True)

        service = Service(executable_path=exec_path)

        self.driver = webdriver.Chrome(service=service, options=chrome_options)

    def stop(self):
        """Closes web driver"""
        try:
            self.driver.close()
        finally:
            self.logged_in = False

    def run(self):
        """Collect data from web portal

        returns (dict, dict): dictionary with statuses and parameters
        """
        # Check if current page is already "main"
        try:
            self.driver.find_element(By.ID, "path7")
            self.logged_in = True
        except (ConnectionError, NoSuchElementException, InvalidSessionIdException):
            self.logged_in = False

        if not self.logged_in:
            self._login_and_navigate()

        # Reading data
        result_status = self._read_status()
        result_settings = self._read_settings()
        self._goto_params_page()
        result_params = self._read_params()

        # Navigate back to main page
        self._back_to_main()

        return result_status, result_params, result_settings

    def _login_and_navigate(self):
        """Navigates from login page to main apge"""
        self._login()
        self._goto_device()
        self._goto_module()
        self._await_main_page()

    def _get_page(self, url):
        """Loads URL content and handles potential errors"""
        try:
            self.driver.get(url)
        except Exception as err:
            raise ConnectionError(f"Unable to reach URL: {url}") from err

        return True

    def _login(self):
        """Service login page"""
        if not self._get_page(self.url):
            return

        login = self._wait_for_element((By.ID, "login"))
        password = self._wait_for_element((By.ID, "pass"))

        login.send_keys(self.username)
        password.send_keys(self.password)

        self.driver.find_element(By.LINK_TEXT, "zaloguj").click()

    def _goto_device(self):
        """Select a device (after login)"""
        self._wait_for_element((By.CLASS_NAME, "ui-body"))

        # Page contains list with available devices
        # I assume here that there is only one device
        elements = self.driver.find_elements(By.TAG_NAME, "li")
        if not elements:
            self.logged_in = False
            raise ConnectionError("Unable to select a device")
        elements[0].click()

    def _goto_module(self):
        """Selects a module in the device.
        We're interested in management module
        """
        self._wait_for_element((By.ID, "start"))
        self.driver.execute_script(
            "loadModule('101','19');"
        )  # TODO check if this is OK

    def _await_main_page(self):
        # Path7 is "home image"
        self._wait_for_element((By.ID, "path7"))

    def _read_status(self):
        """Status of services is encoded in icons' colors"""
        status = {}
        for icon in self.STATUS:
            try:
                read = self.driver.find_element(
                    By.ID, f"{icon}_"
                ).value_of_css_property("fill")
            except (NoSuchElementException, NoSuchAttributeException):
                read = "rgb(0, 0, 0)"

            status[icon] = read

        return status

    def _read_settings(self):
        settings = {}
        for setting in self.SETTINGS:
            try:
                read = self.driver.find_element(By.ID, setting).text
            except NoSuchElementException:
                continue

            settings[setting] = read

        return settings

    def _goto_params_page(self):
        """Parameters table is loaded into DOM at the beginning
        But the values get populated only when it's opened by the user
        """
        try:
            self.driver.find_element(By.ID, "parameters_lbl_").click()
        except (ElementNotInteractableException, NoSuchElementException) as err:
            raise ConnectionError("Error entering params page") from err

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
            try:
                read = self.driver.find_element(By.ID, param).text
            except NoSuchElementException:
                read = "---"
            params[param] = read

        return params

    def _back_to_main(self):
        """Click on "back" button and go to main page"""
        try:
            self.driver.find_element(By.XPATH, '//*[@id="params"]/div[1]/a[2]').click()
        except (ElementNotInteractableException, NoSuchElementException):
            self.logged_in = False

        self._await_main_page()

    def _wait_for_element(self, locator, timeout=5):
        """Waits for an element to be available and gets its value"""
        try:
            element = WebDriverWait(
                self.driver, timeout=timeout, poll_frequency=0.5
            ).until(EC.presence_of_element_located(locator))
        except TimeoutException as err:
            raise ConnectionError(f"Timeout on waiting for {locator}") from err

        return element


if __name__ == "__main__":
    addon = Kospel()

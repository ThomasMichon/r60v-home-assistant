from rocket_r60v.machine import Machine


class FakeMachine:
    def __init__(self):
        self.active_profile = "A"
        self.auto_off = "08:00"
        self.auto_on = "14:00"
        self.brew_boiler_temperature = 220
        self.current_brew_boiler_temperature = 216
        self.current_brew_time: int | None = None
        self.date_time = ""
        self.display = ""
        self.language = "English"
        self.profile_a = "15:4.5 5:9 10:9 0:0 0:0"
        self.profile_b = "10:0 10:0 10:0 0:0 0:0"
        self.profile_c = "20:9 5:6.0 5:3.0 0:0 0:0"
        self.service_boiler = "on"
        self.service_boiler_temperature = 240
        self.current_service_boiler_temperature = 232
        self.standby = "on"
        self.temperature_unit = "Fahrenheit"
        self.total_coffee_count = 500
        self.water_feed = "Reservoir"

    def connect(self) -> None:
        pass

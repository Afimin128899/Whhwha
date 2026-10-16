from aiogram.fsm.state import State, StatesGroup

class SettingsStates(StatesGroup):
    waiting_for_ref_link = State()
    waiting_for_channels = State()
    waiting_for_check_amount = State()
    waiting_for_delays = State()

class AddSessionStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()

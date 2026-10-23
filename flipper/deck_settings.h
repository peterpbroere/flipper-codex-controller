#pragma once
#include <stdbool.h>

bool deck_settings_load(void);
bool deck_settings_save(bool vibration);
bool deck_settings_load_bluetooth(void);
bool deck_settings_save_bluetooth(bool bluetooth);
bool deck_settings_load_yolo(void);
bool deck_settings_save_yolo(bool enabled);

#include "deck_settings.h"

#include <furi.h>
#include <storage/storage.h>
#include <string.h>

#define SETTINGS_FILE APP_DATA_PATH("settings.bin")

bool deck_settings_load(void) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* file = storage_file_alloc(storage);
    uint8_t data[5] = {0};
    bool vibration = true;
    if(storage_file_open(file, SETTINGS_FILE, FSAM_READ, FSOM_OPEN_EXISTING)) {
        if(storage_file_read(file, data, sizeof(data)) == sizeof(data) &&
           memcmp(data, "ADS\1", 4) == 0 && data[4] <= 1)
            vibration = data[4];
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
    return vibration;
}

bool deck_settings_save(bool vibration) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_simply_mkdir(storage, APP_DATA_PATH(""));
    File* file = storage_file_alloc(storage);
    uint8_t data[5] = {'A', 'D', 'S', 1, vibration ? 1 : 0};
    bool saved = false;
    if(storage_file_open(file, SETTINGS_FILE, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        saved = storage_file_write(file, data, sizeof(data)) == sizeof(data);
        saved = storage_file_sync(file) && saved;
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
    return saved;
}

bool deck_settings_load_bluetooth(void) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* file = storage_file_alloc(storage);
    uint8_t data[5] = {0};
    bool bluetooth = false;
    if(storage_file_open(file, APP_DATA_PATH("transport.bin"), FSAM_READ, FSOM_OPEN_EXISTING)) {
        bluetooth = storage_file_read(file, data, sizeof(data)) == sizeof(data) &&
                    memcmp(data, "ADT\1", 4) == 0 && data[4] == 1;
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
    return bluetooth;
}

bool deck_settings_save_bluetooth(bool bluetooth) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_simply_mkdir(storage, APP_DATA_PATH(""));
    File* file = storage_file_alloc(storage);
    uint8_t data[5] = {'A', 'D', 'T', 1, bluetooth ? 1 : 0};
    bool saved = false;
    if(storage_file_open(file, APP_DATA_PATH("transport.bin"), FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        saved = storage_file_write(file, data, sizeof(data)) == sizeof(data);
        saved = storage_file_sync(file) && saved;
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
    return saved;
}

bool deck_settings_load_yolo(void) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* file = storage_file_alloc(storage);
    uint8_t data[5] = {0};
    bool enabled = false;
    if(storage_file_open(file, APP_DATA_PATH("yolo.bin"), FSAM_READ, FSOM_OPEN_EXISTING)) {
        enabled = storage_file_read(file, data, sizeof(data)) == sizeof(data) &&
                  memcmp(data, "ADY\1", 4) == 0 && data[4] == 1;
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
    return enabled;
}

bool deck_settings_save_yolo(bool enabled) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_simply_mkdir(storage, APP_DATA_PATH(""));
    File* file = storage_file_alloc(storage);
    uint8_t data[5] = {'A', 'D', 'Y', 1, enabled ? 1 : 0};
    bool saved = false;
    if(storage_file_open(file, APP_DATA_PATH("yolo.bin"), FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        saved = storage_file_write(file, data, sizeof(data)) == sizeof(data);
        saved = storage_file_sync(file) && saved;
        storage_file_close(file);
    }
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
    return saved;
}

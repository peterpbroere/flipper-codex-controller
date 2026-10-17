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

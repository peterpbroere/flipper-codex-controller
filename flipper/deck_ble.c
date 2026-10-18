#include "deck_ble.h"

#include <bt/bt_service/bt.h>
#include <furi_hal_bt.h>
#include <profiles/serial_profile.h>
#include <services/battery_service.h>
#include <services/dev_info_service.h>
#include <services/serial_service.h>
#include <storage/storage.h>
#include <stdio.h>
#include <string.h>

#define FLAG_STOP  (1U << 0)
#define FLAG_RESET (1U << 1)
#define TX_SIZE    128
#define RX_WINDOW  BLE_SVC_SERIAL_DATA_LEN_MAX

typedef struct {
    uint32_t epoch;
    char line[TX_SIZE];
} Outgoing;

typedef struct {
    FuriHalBleProfileBase base;
    BleServiceSerial* serial;
    BleServiceBattery* battery;
    BleServiceDevInfo* info;
} Profile;

struct DeckBle {
    Bt* bt;
    Profile profile;
    FuriThread* thread;
    FuriMessageQueue* incoming;
    FuriMessageQueue* outgoing;
    FuriStreamBuffer* received;
    FuriSemaphore* sent;
    volatile bool connected;
    volatile bool overflow;
    volatile uint32_t epoch;
    char line[DECK_MAX_LINE];
    size_t used;
    size_t consumed;
    bool dropping;
};

static const FuriHalBleProfileTemplate profile_template;

static uint16_t serial_event(SerialServiceEvent event, void* context) {
    DeckBle* ble = context;
    if(event.event == SerialServiceEventTypeDataReceived) {
        if(furi_stream_buffer_send(ble->received, event.data.buffer, event.data.size, 0) !=
           event.data.size)
            ble->overflow = true;
    } else if(event.event == SerialServiceEventTypeDataSent) {
        furi_semaphore_release(ble->sent);
    }
    return RX_WINDOW;
}

static FuriHalBleProfileBase* profile_start(FuriHalBleProfileParams params) {
    DeckBle* ble = params;
    Profile* profile = &ble->profile;
    profile->base.config = &profile_template;
    profile->info = ble_svc_dev_info_start();
    profile->battery = ble_svc_battery_start(true);
    profile->serial = ble_svc_serial_start();
    furi_check(profile->serial);
    ble_svc_serial_set_callbacks(profile->serial, RX_WINDOW, serial_event, ble);
    return &profile->base;
}

static void profile_stop(FuriHalBleProfileBase* base) {
    Profile* profile = (Profile*)base;
    furi_check(base->config == &profile_template);
    ble_svc_serial_set_callbacks(profile->serial, 0, NULL, NULL);
    ble_svc_serial_stop(profile->serial);
    profile->serial = NULL;
    ble_svc_battery_stop(profile->battery);
    ble_svc_dev_info_stop(profile->info);
}

static void profile_config(GapConfig* config, FuriHalBleProfileParams params) {
    UNUSED(params);
    ble_profile_serial->get_gap_config(config, NULL);
    // A different MAC and bond file preserve the normal phone pairing.
    config->mac_address[2] ^= 0xD3;
    snprintf(
        config->adv_name + 1,
        sizeof(config->adv_name) - 1,
        "Deck %s",
        furi_hal_version_get_name_ptr());
}

static const FuriHalBleProfileTemplate profile_template = {
    .start = profile_start,
    .stop = profile_stop,
    .get_gap_config = profile_config,
};

static void status_changed(BtStatus status, void* context) {
    DeckBle* ble = context;
    bool connected = status == BtStatusConnected;
    if(connected != ble->connected) {
        if(connected)
            ble_svc_serial_set_callbacks(ble->profile.serial, RX_WINDOW, serial_event, ble);
        ble->connected = connected;
        ble->epoch++;
        if(!connected) {
            furi_semaphore_release(ble->sent);
            furi_thread_flags_set(furi_thread_get_id(ble->thread), FLAG_RESET);
        }
    }
}

static bool receive(DeckBle* ble) {
    uint8_t bytes[243];
    size_t size;
    while((size = furi_stream_buffer_receive(ble->received, bytes, sizeof(bytes), 0))) {
        for(size_t i = 0; i < size; i++) {
            uint8_t byte = bytes[i];
            if(byte == '\n') {
                if(ble->used && !ble->dropping) {
                    DeckMessage message = {.type = DeckMessageLine};
                    memcpy(message.data.line, ble->line, ble->used);
                    message.data.line[ble->used] = 0;
                    if(furi_message_queue_put(ble->incoming, &message, 20) != FuriStatusOk)
                        return false;
                }
                ble->used = 0;
                ble->dropping = false;
            } else if(byte != '\r') {
                if(!byte || ble->used >= sizeof(ble->line) - 1)
                    ble->dropping = true;
                else if(!ble->dropping)
                    ble->line[ble->used++] = byte;
            }
        }
        ble->consumed += size;
        if(ble->consumed == RX_WINDOW) {
            ble->consumed = 0;
            ble_svc_serial_notify_buffer_is_empty(ble->profile.serial);
        }
    }
    return true;
}

static int32_t worker(void* context) {
    DeckBle* ble = context;
    while(true) {
        uint32_t flags = furi_thread_flags_wait(FLAG_STOP | FLAG_RESET, FuriFlagWaitAny, 10);
        if(!(flags & FuriFlagError) && (flags & FLAG_STOP)) break;
        if(!(flags & FuriFlagError) && (flags & FLAG_RESET)) {
            ble->used = ble->consumed = 0;
            ble->dropping = false;
            DeckMessage message = {.type = DeckMessageReset};
            furi_message_queue_put(ble->incoming, &message, 20);
        }
        if(!ble->connected) {
            uint8_t discard[64];
            while(furi_stream_buffer_receive(ble->received, discard, sizeof(discard), 0)) {
            }
        } else if(ble->overflow || !receive(ble)) {
            ble->overflow = false;
            bt_disconnect(ble->bt);
        }
        Outgoing outgoing;
        while(furi_message_queue_get(ble->outgoing, &outgoing, 0) == FuriStatusOk) {
            size_t length = strlen(outgoing.line);
            for(size_t offset = 0; offset < length; offset += 20) {
                if(!ble->connected || outgoing.epoch != ble->epoch) break;
                while(furi_semaphore_acquire(ble->sent, 0) == FuriStatusOk) {
                }
                size_t count = MIN((size_t)20, length - offset);
                if(!ble_svc_serial_update_tx(
                       ble->profile.serial, (uint8_t*)outgoing.line + offset, count) ||
                   furi_semaphore_acquire(ble->sent, 500) != FuriStatusOk) {
                    bt_disconnect(ble->bt);
                    break;
                }
            }
        }
    }
    return 0;
}

DeckBle* deck_ble_alloc(FuriMessageQueue* incoming) {
    DeckBle* ble = malloc(sizeof(DeckBle));
    memset(ble, 0, sizeof(*ble));
    ble->incoming = incoming;
    ble->outgoing = furi_message_queue_alloc(8, sizeof(Outgoing));
    ble->received = furi_stream_buffer_alloc(RX_WINDOW, 1);
    ble->sent = furi_semaphore_alloc(1, 0);
    ble->thread = furi_thread_alloc_ex("AgentDeckBle", 2048, worker, ble);
    ble->bt = furi_record_open(RECORD_BT);
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_simply_mkdir(storage, APP_DATA_PATH(""));
    // Reuse the feasibility app's existing bond once, for the same BLE identity.
    if(!storage_file_exists(storage, APP_DATA_PATH("ble.keys")) &&
       storage_file_exists(storage, EXT_PATH("apps_data/agent_deck_ble_test/ble.keys")))
        storage_common_copy(
            storage,
            EXT_PATH("apps_data/agent_deck_ble_test/ble.keys"),
            APP_DATA_PATH("ble.keys"));
    furi_record_close(RECORD_STORAGE);
    bt_disconnect(ble->bt);
    bt_keys_storage_set_storage_path(ble->bt, APP_DATA_PATH("ble.keys"));
    furi_thread_start(ble->thread);
    bt_set_status_changed_callback(ble->bt, status_changed, ble);
    if(!bt_profile_start(ble->bt, &profile_template, ble)) {
        deck_ble_free(ble);
        return NULL;
    }
    furi_hal_bt_start_advertising();
    return ble;
}

void deck_ble_free(DeckBle* ble) {
    if(!ble) return;
    bt_set_status_changed_callback(ble->bt, NULL, NULL);
    bt_disconnect(ble->bt);
    ble->connected = false;
    furi_thread_flags_set(furi_thread_get_id(ble->thread), FLAG_STOP);
    furi_semaphore_release(ble->sent);
    furi_thread_join(ble->thread);
    bt_keys_storage_set_default_path(ble->bt);
    bt_profile_restore_default(ble->bt);
    furi_record_close(RECORD_BT);
    furi_thread_free(ble->thread);
    furi_message_queue_free(ble->outgoing);
    furi_stream_buffer_free(ble->received);
    furi_semaphore_free(ble->sent);
    free(ble);
}

bool deck_ble_send(DeckBle* ble, const char* line) {
    if(!ble || !ble->connected || strlen(line) >= TX_SIZE) return false;
    Outgoing outgoing = {.epoch = ble->epoch};
    strcpy(outgoing.line, line);
    return furi_message_queue_put(ble->outgoing, &outgoing, 0) == FuriStatusOk;
}

bool deck_ble_connected(DeckBle* ble) {
    return ble && ble->connected;
}

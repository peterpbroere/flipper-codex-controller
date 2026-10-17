#include "deck_usb.h"

#include <furi_hal_usb.h>
#include <furi_hal_usb_cdc.h>
#include <string.h>

#define DECK_USB_CHANNEL 1
#define USB_FLAG_STOP    (1UL << 0)
#define USB_FLAG_RX      (1UL << 1)
#define USB_FLAG_TX      (1UL << 2)
#define USB_FLAG_RESET   (1UL << 3)
#define TX_LINE_SIZE     128

struct DeckUsb {
    FuriThread* thread;
    FuriMessageQueue* incoming;
    FuriMessageQueue* outgoing;
    FuriSemaphore* tx_ready;
    FuriHalUsbInterface* previous;
    char line[DECK_MAX_LINE];
    size_t used;
    bool dropping;
};

static void usb_rx(void* context) {
    DeckUsb* usb = context;
    furi_thread_flags_set(furi_thread_get_id(usb->thread), USB_FLAG_RX);
}

static void usb_tx(void* context) {
    DeckUsb* usb = context;
    furi_semaphore_release(usb->tx_ready);
}

static void usb_state(void* context, CdcState state) {
    DeckUsb* usb = context;
    if(state == CdcStateDisconnected) {
        furi_thread_flags_set(furi_thread_get_id(usb->thread), USB_FLAG_RESET);
        furi_semaphore_release(usb->tx_ready);
    }
}

static void usb_control(void* context, CdcCtrlLine lines) {
    DeckUsb* usb = context;
    UNUSED(lines);
    furi_semaphore_release(usb->tx_ready);
}

static CdcCallbacks callbacks = {
    .tx_ep_callback = usb_tx,
    .rx_ep_callback = usb_rx,
    .state_callback = usb_state,
    .ctrl_line_callback = usb_control,
};

static void receive_bytes(DeckUsb* usb) {
    uint8_t bytes[CDC_DATA_SZ];
    int32_t size;
    while((size = furi_hal_cdc_receive(DECK_USB_CHANNEL, bytes, sizeof(bytes))) > 0) {
        for(int32_t i = 0; i < size; i++) {
            uint8_t byte = bytes[i];
            if(byte == '\n') {
                if(!usb->dropping && usb->used) {
                    usb->line[usb->used] = '\0';
                    DeckMessage message = {.type = DeckMessageLine};
                    memcpy(message.data.line, usb->line, usb->used + 1);
                    furi_message_queue_put(usb->incoming, &message, 20);
                }
                usb->used = 0;
                usb->dropping = false;
            } else if(byte != '\r') {
                if(usb->used >= sizeof(usb->line) - 1 || byte == 0) {
                    usb->dropping = true;
                } else if(!usb->dropping) {
                    usb->line[usb->used++] = (char)byte;
                }
            }
        }
    }
}

static int32_t usb_worker(void* context) {
    DeckUsb* usb = context;
    while(true) {
        uint32_t flags = furi_thread_flags_wait(
            USB_FLAG_STOP | USB_FLAG_RX | USB_FLAG_TX | USB_FLAG_RESET, FuriFlagWaitAny, 50);
        if(!(flags & FuriFlagError) && (flags & USB_FLAG_STOP)) break;
        if(!(flags & FuriFlagError) && (flags & USB_FLAG_RESET)) {
            usb->used = 0;
            usb->dropping = false;
        }
        receive_bytes(usb);
        char line[TX_LINE_SIZE];
        while(furi_message_queue_get(usb->outgoing, line, 0) == FuriStatusOk) {
            if(!(furi_hal_cdc_get_ctrl_line_state(DECK_USB_CHANNEL) & CdcCtrlLineDTR)) continue;
            size_t length = strlen(line);
            for(size_t offset = 0; offset < length; offset += CDC_DATA_SZ) {
                if(furi_semaphore_acquire(usb->tx_ready, 250) != FuriStatusOk) break;
                size_t size = length - offset;
                if(size > CDC_DATA_SZ) size = CDC_DATA_SZ;
                furi_hal_cdc_send(DECK_USB_CHANNEL, (uint8_t*)line + offset, size);
            }
        }
    }
    return 0;
}

DeckUsb* deck_usb_alloc(FuriMessageQueue* incoming) {
    if(furi_hal_usb_is_locked() || furi_hal_usb_get_config() != &usb_cdc_single) return NULL;
    DeckUsb* usb = malloc(sizeof(DeckUsb));
    memset(usb, 0, sizeof(*usb));
    usb->incoming = incoming;
    usb->previous = furi_hal_usb_get_config();
    usb->outgoing = furi_message_queue_alloc(8, TX_LINE_SIZE);
    usb->tx_ready = furi_semaphore_alloc(1, 1);
    usb->thread = furi_thread_alloc_ex("AgentDeckUsb", 2048, usb_worker, usb);
    furi_thread_start(usb->thread);
    if(!furi_hal_usb_set_config(&usb_cdc_dual, NULL)) {
        furi_thread_flags_set(furi_thread_get_id(usb->thread), USB_FLAG_STOP);
        furi_thread_join(usb->thread);
        furi_thread_free(usb->thread);
        furi_semaphore_free(usb->tx_ready);
        furi_message_queue_free(usb->outgoing);
        free(usb);
        return NULL;
    }
    furi_hal_cdc_set_callbacks(DECK_USB_CHANNEL, &callbacks, usb);
    return usb;
}

void deck_usb_free(DeckUsb* usb) {
    if(!usb) return;
    furi_hal_cdc_set_callbacks(DECK_USB_CHANNEL, NULL, NULL);
    furi_thread_flags_set(furi_thread_get_id(usb->thread), USB_FLAG_STOP);
    furi_thread_join(usb->thread);
    furi_thread_free(usb->thread);
    if(!furi_hal_usb_is_locked() && furi_hal_usb_get_config() == &usb_cdc_dual) {
        furi_hal_usb_set_config(usb->previous, NULL);
    }
    furi_semaphore_free(usb->tx_ready);
    furi_message_queue_free(usb->outgoing);
    free(usb);
}

bool deck_usb_send(DeckUsb* usb, const char* line) {
    if(!usb || strlen(line) >= TX_LINE_SIZE) return false;
    char copy[TX_LINE_SIZE] = {0};
    strcpy(copy, line);
    if(furi_message_queue_put(usb->outgoing, copy, 0) != FuriStatusOk) return false;
    furi_thread_flags_set(furi_thread_get_id(usb->thread), USB_FLAG_TX);
    return true;
}

#pragma once

#include <furi.h>
#include <input/input.h>
#include "deck_protocol.h"

typedef enum {
    DeckMessageInput,
    DeckMessageLine
} DeckMessageType;

typedef struct {
    DeckMessageType type;
    union {
        InputEvent input;
        char line[DECK_MAX_LINE];
    } data;
} DeckMessage;

typedef struct DeckUsb DeckUsb;

DeckUsb* deck_usb_alloc(FuriMessageQueue* incoming);
void deck_usb_free(DeckUsb* usb);
bool deck_usb_send(DeckUsb* usb, const char* line);

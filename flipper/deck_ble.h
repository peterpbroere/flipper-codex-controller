#pragma once
#include "deck_usb.h"

typedef struct DeckBle DeckBle;
DeckBle* deck_ble_alloc(FuriMessageQueue* incoming);
void deck_ble_free(DeckBle* ble);
bool deck_ble_send(DeckBle* ble, const char* line);
bool deck_ble_connected(DeckBle* ble);

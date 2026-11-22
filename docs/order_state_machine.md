# Order State Machine

Orders use CREATED, SUBMITTING, PENDING_CONFIRMATION, ACCEPTED, PARTIALLY_FILLED, CANCEL_PENDING, FILLED, CANCELED, REJECTED, CONFIRMED_NOT_ACCEPTED, and EXPIRED. Timeout must remain PENDING_CONFIRMATION until authoritative KIS inquiry evidence confirms acceptance or explicit not-accepted status.

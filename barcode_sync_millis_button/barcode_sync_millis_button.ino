/*
  Based in part on code from the: 
    Optogenetics and Neural Engineering Core ONE Core
    University of Colorado, School of Medicine
    31.Oct.2021
    See bit.ly/onecore for more information, including a more detailed write up.
    arduino_barcodes.ino

  Modified to do both DAQ synchronization with randomized 32 bit digital barcodes
  and emit synchronised timing pulses for triggering cameras to capture frames.

  Serial command interface:
    'A' — Start barcodes + camera TTLs immediately (manual / legacy)
    'S' — Stop barcodes + camera TTLs immediately (manual / legacy)
    'B' — Start barcodes only
    'D' — Stop barcodes only
    'C' — Start camera TTLs only
    'E' — Stop camera TTLs only
    'R' — Recording active: start barcodes immediately, queue camera TTLs to start
           after the next full barcode completes (so first cam TTL is always inside
           a clean barcode boundary and inside the ephys recording)
    'X' — Recording ending: stop camera TTLs immediately, let current barcode
           finish cleanly before stopping barcodes (ephys file stays open until
           after the last barcode is complete)
    '?' — Print current status over serial (used by Python startup handshake)
*/

////// Timers ////////
unsigned long currentMillis = 0;
unsigned long currentMicros = 0;
bool runCam     = false;
bool runBarcode = false;

// 'R' command: camera start is deferred until the next barcode boundary
bool camStartPending = false;

// 'X' command: barcodes finish their current cycle before stopping
bool barcodeStopPending = false;

//////// SETUP Input ////////
const int ttlPin = 8;

//////// SETUP BARCODE ////////
const int barcodePin               = 9;
const int ledPin                   = 13;
const int randomPin                = A0;
const int barcodeButtonPin         = 7;
const int barcodeButtonDebounce    = 100;
const int barcodeButtonReactivate  = 1000;

const int barcodeBits      = 32;
const int barcodeInitLength = 3;
const int barcodeInitTime  = 10;
const int barcodePulse     = 30;
const int barcodeTotalTime = (2 * (barcodeInitLength * barcodeInitTime)) + (barcodePulse * barcodeBits);
const int barcodeInterval  = 10000;
const int barcodeWaitTime  = barcodeInterval - barcodeTotalTime;

unsigned long previousBarcode           = 0;
unsigned long previousBarcodeButton     = 0;
unsigned long previousBarcodeActivation = 0;
int barcode            = 0;
int barcodeCounter     = 0;
int barcodeDuration    = 0;
int barcodeState       = 1; // 1=init, 2=run, 3=exit, 4=wait
int barcodeInitCounter = 0;
int barcodeExitCounter = 0;
int barcodeDigit;
bool barcodeButtonState = 0;

//////// SETUP CAM ////////////
const int camInterval          = 2500; // microseconds between triggers (400 Hz)
const int camPin               = 10;
const int camHigh              = 1000; // pulse high time in microseconds
const int camLow               = 1500; // pulse low time in microseconds
const int camButtonPin         = 6;
const int camButtonDebounce    = 100;
const int camButtonReactivate  = 1000;

unsigned long previousCam           = 0;
unsigned long previousCamButton     = 0;
unsigned long previousCamActivation = 0;
int  camDuration    = 1000;
bool camState       = false;
bool camButtonState = 0;

// =====================================================================
void setup() {
  Serial.begin(9600);
  Serial.println("Serial Initialised...");

  pinMode(barcodePin,       OUTPUT); digitalWrite(barcodePin, LOW);
  pinMode(camPin,           OUTPUT); digitalWrite(camPin,     LOW);
  pinMode(ledPin,           OUTPUT); digitalWrite(ledPin,     HIGH);
  pinMode(barcodeButtonPin, INPUT_PULLUP);
  pinMode(camButtonPin,     INPUT_PULLUP);

  randomSeed(analogRead(randomPin));
  barcode = random(0, pow(2, barcodeBits));

  printStatus();
}

// =====================================================================
void loop() {
  int r = Serial.read();
  switch (r) {

    case 'A': // legacy: start everything immediately
      if (!runBarcode) { runBarcode = true; Serial.println("Running Barcode Pulses..."); }
      if (!runCam)     { runCam     = true; Serial.println("Running Camera TTL Pulses..."); }
      camStartPending    = false;
      barcodeStopPending = false;
      break;

    case 'S': // legacy: stop everything immediately
      if (runBarcode) { runBarcode = false; Serial.println("Stopped Barcode Pulses."); }
      if (runCam)     { runCam     = false; Serial.println("Stopped Camera TTL Pulses."); }
      camStartPending    = false;
      barcodeStopPending = false;
      ensurePinsLow();
      break;

    case 'B': // barcodes only - start
      if (!runBarcode) { runBarcode = true; Serial.println("Running Barcode TTL Pulses..."); }
      break;

    case 'D': // barcodes only - stop
      if (runBarcode) { runBarcode = false; Serial.println("Stopping Barcode TTL Pulses..."); }
      break;

    case 'C': // camera only - start
      if (!runCam) { runCam = true; Serial.println("Running Camera TTL Pulses..."); }
      camStartPending = false;
      break;

    case 'E': // camera only - stop
      if (runCam) { runCam = false; Serial.println("Stopping Camera TTL Pulses..."); }
      break;

    case 'R': // === Recording active ===
      // Start barcodes immediately so ephys file captures a clean barcode at the start
      if (!runBarcode) {
        runBarcode = true;
        Serial.println("R: Barcodes started.");
      }
      // Defer camera TTLs to next clean barcode boundary
      if (!runCam && !camStartPending) {
        camStartPending = true;
        Serial.println("R: Camera TTLs queued - will start after current barcode cycle.");
      } else if (runCam) {
        Serial.println("R: Camera TTLs already running.");
      }
      barcodeStopPending = false;
      break;

    case 'X': // === Recording ending ===
      // Stop camera TTLs immediately - ephys file is still open
      if (runCam) {
        runCam   = false;
        camState = false;
        digitalWrite(camPin, LOW);
        Serial.println("X: Camera TTLs stopped.");
      }
      camStartPending = false;
      // Let current barcode finish so ephys gets a clean final barcode
      if (runBarcode && !barcodeStopPending) {
        barcodeStopPending = true;
        Serial.println("X: Barcodes will stop after current cycle completes.");
      } else if (!runBarcode) {
        Serial.println("X: Barcodes already stopped.");
      }
      break;

    case '?': // status query - used by Python startup handshake
      printStatus();
      break;

    default:
      break;
  }

  currentMillis = millis();
  updateBarcodeButton();
  updateBarcode();
  updateCamButton();
  currentMicros = micros();
  updateCam();
}

// =====================================================================
// Helpers
// =====================================================================

void printStatus() {
  Serial.print("STATUS barcode=");
  Serial.print(runBarcode ? "1" : "0");
  Serial.print(" cam=");
  Serial.print(runCam ? "1" : "0");
  Serial.print(" camPending=");
  Serial.print(camStartPending ? "1" : "0");
  Serial.print(" barcodeStopPending=");
  Serial.println(barcodeStopPending ? "1" : "0");
}

void ensurePinsLow() {
  digitalWrite(camPin,     LOW);
  digitalWrite(barcodePin, LOW);
}

// =====================================================================
// Button handlers
// =====================================================================

void updateBarcodeButton() {
  if ( ((currentMillis - previousBarcodeButton)     >= barcodeButtonDebounce) &&
       ((currentMillis - previousBarcodeActivation) >= barcodeButtonReactivate) ) {
    barcodeButtonState = digitalRead(barcodeButtonPin);
    if (barcodeButtonState == LOW) {
      Serial.println("Barcode Button pressed.");
      runBarcode = !runBarcode;
      Serial.println(runBarcode ? "Starting Barcode TTL Pulses..." : "Stopping Barcode TTL Pulses...");
      previousBarcodeActivation = currentMillis;
    }
    previousBarcodeButton = currentMillis;
  }
}

void updateCamButton() {
  if ( ((currentMillis - previousCamButton)       >= camButtonDebounce) &&
       ((currentMillis - previousCamActivation)   >= camButtonReactivate) ) {
    camButtonState = digitalRead(camButtonPin);
    if (camButtonState == LOW) {
      Serial.println("Cam Button pressed.");
      runCam          = !runCam;
      camStartPending = false; // manual button overrides any pending state
      Serial.println(runCam ? "Starting Camera TTL Pulses..." : "Stopping Camera TTL Pulses...");
      previousCamActivation = currentMillis;
    }
    previousCamButton = currentMillis;
  }
}

// =====================================================================
// Camera pulse generator
// =====================================================================

void updateCam() {
  if ((currentMicros - previousCam) >= camDuration) {
    if (camState) {
      // Currently high - go low
      digitalWrite(camPin, LOW);
      camDuration = camLow;
      camState    = false;
    } else if (runCam) {
      // Currently low and running - go high
      digitalWrite(camPin, HIGH);
      camDuration = camHigh;
      camState    = true;
    }
    previousCam = currentMicros;
  }
}

// =====================================================================
// Barcode generator
//
// State 4 (inter-barcode wait) is the safe point for two deferred actions:
//   camStartPending    -> start camera TTLs now (clean boundary, inside ephys)
//   barcodeStopPending -> stop barcodes now (clean boundary, ephys still open)
// =====================================================================

void updateBarcode() {
  if ((currentMillis - previousBarcode) >= barcodeDuration) {
    previousBarcode = currentMillis;

    switch (barcodeState) {

      case 1: // Initialise - wrap pulses before barcode
        if (runBarcode) {
          digitalWrite(ledPin, LOW);
          switch (barcodeInitCounter) {
            case 0:
              digitalWrite(barcodePin, LOW);
              barcodeDuration    = barcodeInitTime;
              barcodeInitCounter = 1;
              break;
            case 1:
              digitalWrite(barcodePin, HIGH);
              barcodeDuration    = barcodeInitTime;
              barcodeInitCounter = 2;
              break;
            case 2:
              digitalWrite(barcodePin, LOW);
              barcodeDuration    = barcodeInitTime;
              barcodeInitCounter = 0;
              barcodeState = 2;
              break;
          }
        }
        break;

      case 2: // Run barcode bits
        barcodeDigit = bitRead(barcode >> barcodeCounter, 0);
        digitalWrite(barcodePin, barcodeDigit ? HIGH : LOW);
        barcodeDuration = barcodePulse;
        barcodeCounter++;
        if (barcodeCounter == barcodeBits) {
          barcode++;
          barcodeState   = 3;
          barcodeCounter = 0;
        }
        break;

      case 3: // Exit - wrap pulses after barcode
        switch (barcodeExitCounter) {
          case 0:
            digitalWrite(barcodePin, LOW);
            barcodeDuration    = barcodeInitTime;
            barcodeExitCounter = 1;
            break;
          case 1:
            digitalWrite(barcodePin, HIGH);
            barcodeDuration    = barcodeInitTime;
            barcodeExitCounter = 2;
            break;
          case 2:
            digitalWrite(barcodePin, LOW);
            barcodeDuration    = barcodeInitTime;
            barcodeExitCounter = 0;
            barcodeState = 4;
            break;
        }
        break;

      case 4: // Inter-barcode wait - safe point for deferred actions
        digitalWrite(ledPin, HIGH);

        // Deferred: start camera TTLs at a clean barcode boundary
        if (camStartPending) {
          runCam          = true;
          camStartPending = false;
          Serial.println("Camera TTLs started at barcode boundary.");
        }

        // Deferred: stop barcodes at a clean boundary
        if (barcodeStopPending) {
          runBarcode         = false;
          barcodeStopPending = false;
          digitalWrite(barcodePin, LOW);
          digitalWrite(ledPin,     LOW);
          Serial.println("Barcodes stopped cleanly after cycle.");
          // Reset state machine so next 'B' or 'R' starts fresh
          barcodeState    = 1;
          barcodeCounter  = 0;
          barcodeDuration = 0;
          break;
        }

        barcodeDuration = barcodeWaitTime;
        barcodeState    = 1;
        break;
    }
  }
}

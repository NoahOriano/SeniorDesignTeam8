## Relevant Links
Docs for wifi device control commands: https://docs.espressif.com/projects/esp-at/en/latest/esp32/AT_Command_Set/index.html
ESP32 bootloaders: https://docs.espressif.com/projects/esp-at/en/latest/esp32/AT_Binary_Lists/esp_at_binaries.html

## Current State
1. Prototype UI tested against local server, not connected to device wifi chip. Doesn't look great but tests TCP connection and temp data retrieval.
2. Bootloader and wifi control library identified for use of ESP32 as wifi peripheral
3. Code capable of retrieving data from sensors, and displaying temperatures from sensors on LCD. Responsive to button input; Capable of enabling and disabling each sensor, and displaying average output.
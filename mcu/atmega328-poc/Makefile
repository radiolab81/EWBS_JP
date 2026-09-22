TARGET = main
MCU = atmega328p
F_CPU = 16000000UL
PORT = /dev/ttyUSB0
#BAUD = 115200 
PROGRAMMER = arduino
# WICHTIG: Hier die 57600 für den alten Bootloader eintragen!
BAUD = 57600 

CC = avr-gcc
OBJCOPY = avr-objcopy
CFLAGS = -Wall -Os -DF_CPU=$(F_CPU) -mmcu=$(MCU) -std=gnu99

all: $(TARGET).hex

$(TARGET).elf: $(TARGET).c
	$(CC) $(CFLAGS) -o $(TARGET).elf $(TARGET).c

$(TARGET).hex: $(TARGET).elf
	$(OBJCOPY) -O ihex -R .eeprom $(TARGET).elf $(TARGET).hex

flash: $(TARGET).hex
	avrdude -F -V -c $(PROGRAMMER) -p $(MCU) -P $(PORT) -b $(BAUD) -U flash:w:$(TARGET).hex:i

clean:
	rm -f $(TARGET).elf $(TARGET).hex

monitor:
	stty -F $(PORT) 9600 cs8 -cstopb -parenb raw clocal -echo
	cat $(PORT)
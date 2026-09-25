import time
import spidev
import logging
import numpy as np
from gpiozero import *


SPI_Freq = 80000000     # SPI 时钟频率
SPI_Mode = 0            # 模式0
BL_Freq  = 1000         # PWM 频率（背光）
RST_PIN  = 27
DC_PIN   = 25
BL_PIN   = 18



class jd9853():
    def __init__(self):
        self.np=np
        self.width  = 172
        self.height = 320 
        
        self.GPIO_RST_PIN = DigitalOutputDevice(RST_PIN,active_high = True,initial_value =True)    # RST 设置为输出 参数：引脚，高电平有效，默认高          # 使用GPIO Zero库中的DigitalOutputDevice类
        self.GPIO_DC_PIN  = DigitalOutputDevice(DC_PIN,active_high = True,initial_value =True)     # DC 设置为输出 参数：引脚，高电平有效，默认高           # 使用GPIO Zero库中的DigitalOutputDevice类
        self.GPIO_BL_PIN  = PWMOutputDevice(BL_PIN,frequency = BL_Freq)                            # BL 设置为PWM  参数：引脚，PWM 频率                    # 使用GPIO Zero库中的PWMOutputDevice类
        self.bl_DutyCycle(100)    
        #Initialize SPI
        self.SPI = spidev.SpiDev(0,0)
        self.SPI.max_speed_hz = SPI_Freq  
        self.SPI.mode = 0b00   
        
        self.lcd_init()
    
    def bl_DutyCycle(self, duty):                   # 设置 PWM 占空比
        self.GPIO_BL_PIN.value = duty / 100

    
    
    def digital_write(self, Pin, value):
        if value:
            Pin.on()
        else:
            Pin.off()
            
    def spi_writebyte(self, data):
        if self.SPI!=None :
            self.SPI.writebytes(data)
    
    def command(self, cmd):
        self.digital_write(self.GPIO_DC_PIN, False)
        self.spi_writebyte([cmd])   
        
    def data(self, val):
        self.digital_write(self.GPIO_DC_PIN, True)
        self.spi_writebyte([val])  
        
    def reset(self):
        """Reset the display"""
        self.digital_write(self.GPIO_RST_PIN,True)
        time.sleep(0.01)
        self.digital_write(self.GPIO_RST_PIN,False)
        time.sleep(0.01)
        self.digital_write(self.GPIO_RST_PIN,True)
        time.sleep(0.02)
    
    def dre_rectangle(self, Xstart, Ystart, Xend, Yend, color):
        color_high = (color >> 8) & 0xFF
        color_low = color & 0xFF
            
        self.set_windows( Xstart, Ystart, Xend, Yend) 
        for a in range (Xstart, Xend+1):
            for b in range (Ystart , Yend + 1):
                self.data(color_high)
                self.data(color_low)
    
    def lcd_init(self):
        self.reset()
        time.sleep(10 / 1000.0)
    
        self.command(0x11) 

        time.sleep(120 / 1000.0) 

        self.command(0xDF) 
        self.data(0x98) 
        self.data(0x53) 

        self.command(0xDF) 
        self.data(0x98) 
        self.data(0x53) 

        self.command(0xB2) 
        self.data(0x23) 

        self.command(0x36)   
        self.data(0x00)  

        self.command(0xB7) 
        self.data(0x00) 
        self.data(0x47) 
        self.data(0x00) 
        self.data(0x6F) 

        self.command(0xBB) 
        self.data(0x1C) 
        self.data(0x1A) 
        self.data(0x55) 
        self.data(0x73) 
        self.data(0x63) 
        self.data(0xF0) 

        self.command(0xC0) 
        self.data(0x44) 
        self.data(0xA4) 

        self.command(0xC1) 
        self.data(0x16) 

        self.command(0xC3) 
        self.data(0x7D) 
        self.data(0x07) 
        self.data(0x14) 
        self.data(0x06) 
        self.data(0xCF) 
        self.data(0x71) 
        self.data(0x72) 
        self.data(0x77) 

        self.command(0xC4) 
        self.data(0x00) 
        self.data(0x00) 
        self.data(0xA0) 
        self.data(0x79) 
        self.data(0x0B) 
        self.data(0x0A) 
        self.data(0x16) 
        self.data(0x79) 
        self.data(0x0B) 
        self.data(0x0A) 
        self.data(0x16) 
        self.data(0x82) 

        self.command(0xC8) 
        self.data(0x3F) 
        self.data(0x32) 
        self.data(0x29) 
        self.data(0x29) 
        self.data(0x27) 
        self.data(0x2B) 
        self.data(0x27) 
        self.data(0x28) 
        self.data(0x28) 
        self.data(0x26) 
        self.data(0x25) 
        self.data(0x17) 
        self.data(0x12) 
        self.data(0x0D) 
        self.data(0x04) 
        self.data(0x00) 
        self.data(0x3F) 
        self.data(0x32) 
        self.data(0x29) 
        self.data(0x29) 
        self.data(0x27) 
        self.data(0x2B) 
        self.data(0x27) 
        self.data(0x28) 
        self.data(0x28) 
        self.data(0x26) 
        self.data(0x25) 
        self.data(0x17) 
        self.data(0x12) 
        self.data(0x0D) 
        self.data(0x04) 
        self.data(0x00) 

        self.command(0xD0) 
        self.data(0x04) 
        self.data(0x06) 
        self.data(0x6B) 
        self.data(0x0F) 
        self.data(0x00) 

        self.command(0xD7) 
        self.data(0x00) 
        self.data(0x30) 

        self.command(0xE6) 
        self.data(0x14) 

        self.command(0xDE) 
        self.data(0x01) 

        self.command(0xB7) 
        self.data(0x03) 
        self.data(0x13) 
        self.data(0xEF) 
        self.data(0x35) 
        self.data(0x35) 

        self.command(0xC1) 
        self.data(0x14) 
        self.data(0x15) 
        self.data(0xC0) 

        self.command(0xC2) 
        self.data(0x06) 
        self.data(0x3A) 

        self.command(0xC4) 
        self.data(0x72) 
        self.data(0x12) 

        self.command(0xBE) 
        self.data(0x00) 

        self.command(0xDE) 
        self.data(0x02) 

        self.command(0xE5) 
        self.data(0x00) 
        self.data(0x02) 
        self.data(0x00) 

        self.command(0xE5) 
        self.data(0x01) 
        self.data(0x02) 
        self.data(0x00) 

        self.command(0xDE) 
        self.data(0x00) 

        self.command(0x35) 
        self.data(0x00) 

        self.command(0x3A) 
        self.data(0x05)  # 06=RGB666；05=RGB565

        self.command(0x2A) 
        self.data(0x00) 
        self.data(0x22) # Start_X=34
        self.data(0x00) 
        self.data(0xCD) # End_X=205

        self.command(0x2B) 
        self.data(0x00) 
        self.data(0x00) # /Start_Y=0
        self.data(0x01) 
        self.data(0x3F) # End_Y=319

        self.command(0xDE) 
        self.data(0x02) 

        self.command(0xE5) 
        self.data(0x00) 
        self.data(0x02) 
        self.data(0x00) 

        self.command(0xDE) 
        self.data(0x00) 

        # self.command(0xC2) 
        # self.data(0x08) 
        self.command(0x21) 
        time.sleep(10 / 1000.0) 
        self.command(0x29) 
        time.sleep(10 / 1000.0) 

        
    def set_windows(self, Xstart, Ystart, Xend, Yend, horizontal = 0):
        Xend = Xend - 1
        Yend = Yend - 1

        # The +34 compensates for the panel's physical column addressing (the driver IC's
        # addressable area is wider than the visible 172px; see lcd_init()'s own Start_X=34).
        # In portrait (horizontal=0) that's the X axis, sent via 0x2A, unchanged below. In
        # landscape (horizontal=1) show_image() sets MADCTL to 0x70, which sets the MV
        # (row/column exchange) bit: 0x2A now addresses what was physically the row
        # direction (320px, no offset needed) and 0x2B addresses what was physically the
        # column direction (172px, THE one needing +34) — i.e. the offset has to move to Y,
        # not stay on X, once the axes are swapped. It was unconditionally on X before this
        # fix, landscape-only, which is why show_image() was corrupted here: real image
        # content was being windowed into the wrong hardware columns, not just the double-
        # write bug fixed earlier (that alone wasn't sufficient — this is the second bug on
        # the same landscape path).
        if horizontal:
            Ystart = Ystart + 34
            Yend = Yend + 34
        else:
            Xstart = Xstart + 34
            Xend = Xend + 34
        if horizontal:
            #set the X coordinates
            self.command(0x2A)
            self.data(Xstart>>8)         #Set the horizontal starting point to the high octet
            self.data(Xstart & 0xff)     #Set the horizontal starting point to the low octet
            self.data(Xend>>8)         #Set the horizontal end to the high octet
            self.data((Xend) & 0xff)   #Set the horizontal end to the low octet 
            #set the Y coordinates
            self.command(0x2B)
            self.data(Ystart>>8)
            self.data((Ystart & 0xff))
            self.data(Yend>>8)
            self.data((Yend) & 0xff)
            self.command(0x2C)
        else:
            #set the X coordinates
            self.command(0x2A)
            self.data(Xstart>>8)        #Set the horizontal starting point to the high octet
            self.data(Xstart & 0xff)    #Set the horizontal starting point to the low octet
            self.data(Xend>>8)        #Set the horizontal end to the high octet
            self.data((Xend) & 0xff)  #Set the horizontal end to the low octet 
            #set the Y coordinates
            self.command(0x2B)
            self.data(Ystart>>8)
            self.data((Ystart & 0xff))
            self.data(Yend>>8)
            self.data((Yend) & 0xff)
            self.command(0x2C)     
    
    
    def show_image_windows(self, Xstart, Ystart, Xend, Yend, Image):

        # """Set buffer to value of Python Imaging Library image."""
        # """Write display buffer to physical display"""
        imwidth, imheight = Image.size
        if imwidth != self.width or imheight != self.height:
            raise ValueError('Image must be same dimensions as display \
                ({0}x{1}).' .format(self.width, self.height))
        img = self.np.asarray(Image)
        pix = self.np.zeros((imheight,imwidth , 2), dtype = self.np.uint8)
        #RGB888 >> RGB565
        pix[...,[0]] = self.np.add(self.np.bitwise_and(img[...,[0]],0xF8),self.np.right_shift(img[...,[1]],5))
        pix[...,[1]] = self.np.add(self.np.bitwise_and(self.np.left_shift(img[...,[1]],3),0xE0), self.np.right_shift(img[...,[2]],3))
        pix = pix.flatten().tolist()
            
        if Xstart > Xend:
            data = Xstart
            Xstart = Xend
            Xend = data
            
        if Ystart > Yend:        
            data = Ystart
            Ystart = Yend
            Yend = data
        
        if Xend < self.width - 1:
            Xend = Xend + 1
        if Yend < self.width - 1:
            Yend = Yend + 1
            
        self.set_windows( Xstart, Ystart, Xend, Yend)
        self.digital_write(self.GPIO_DC_PIN,True)
        
        for i in range (Ystart,Yend):             
            Addr = ((Xstart) + (i * 240)) * 2        
            self.spi_writebyte(pix[Addr : Addr+((Xend-Xstart+1)*2)])

    def show_image(self, Image):
        """Set buffer to value of Python Imaging Library image."""
        """Write display buffer to physical display"""
        imwidth, imheight = Image.size
        if imwidth == self.height and imheight ==  self.width:
            # print("Landscape screen")
            img = self.np.asarray(Image)
            pix = self.np.zeros((self.width, self.height,2), dtype = self.np.uint8)
            #RGB888 >> RGB565
            pix[...,[0]] = self.np.add(self.np.bitwise_and(img[...,[0]],0xF8),self.np.right_shift(img[...,[1]],5))
            pix[...,[1]] = self.np.add(self.np.bitwise_and(self.np.left_shift(img[...,[1]],3),0xE0), self.np.right_shift(img[...,[2]],3))
            pix = pix.flatten().tolist()
            
            self.command(0x36)
            self.data(0x70)
            self.set_windows(0, 0, self.height,self.width, 1)
            self.digital_write(self.GPIO_DC_PIN,True)
            # (Was its own for-loop here, writing `pix` once — then the branch fell through
            # to the SAME unconditional loop at the bottom of this method, writing it AGAIN
            # with no set_windows() re-issued in between: a double write with no window
            # reset, landscape-only. Found live: real corruption on the actual panel,
            # portrait mode was already fine since it never had this duplicate loop.
            # Removed; the trailing loop below now does the one real write for both modes,
            # same as it always did for portrait.)
        else :
            # print("Portrait screen")
            img = self.np.asarray(Image)
            pix = self.np.zeros((imheight,imwidth , 2), dtype = self.np.uint8)
            
            pix[...,[0]] = self.np.add(self.np.bitwise_and(img[...,[0]],0xF8),self.np.right_shift(img[...,[1]],5))
            pix[...,[1]] = self.np.add(self.np.bitwise_and(self.np.left_shift(img[...,[1]],3),0xE0), self.np.right_shift(img[...,[2]],3))
            pix = pix.flatten().tolist()
            
            self.command(0x36)
            self.data(0x00)
            self.set_windows(0, 0, self.width, self.height, 0)
            self.digital_write(self.GPIO_DC_PIN,True)
        for i in range(0, len(pix), 4096):
            self.spi_writebyte(pix[i: i+4096])

    
    def clear(self):
        """Clear contents of image buffer"""
        _buffer = [0xff] * (self.width*self.height*2)
        self.set_windows(0, 0, self.width, self.height)
        self.digital_write(self.GPIO_DC_PIN,True)
        for i in range(0, len(_buffer), 4096):
            self.spi_writebyte(_buffer[i: i+4096])
    
    

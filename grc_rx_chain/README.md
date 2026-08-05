# 欢迎来到GNU Radio的世界。  
唉呀，我这个硬件在别的地方说不上什么话，到这个文件夹可算是能给你掰扯上一些东西了。  
  
文件结构如下：  
`gr_rx_chain.py #总block`  
`|  epy_gfsk_demod.py #GFSK解调`  
`|  epy_protocol_block.py #空中包——协议帧转换`  
  
不清楚空中包的结构的娃们请移步：https://bbs.robomaster.com/wiki/20204847/809871?source=7  
  
首先是GFSK解调。**这部分不能直接用GRC自带的GFSK Demod。不然你会消失的。** 因为GRC自带模块在包中已经为你做好了0 1判决，所以使用自带模块无法解决归一化、去直流、生成抽头的问题。  
说实话，之前其实一直对鉴频器的作用感到不解，况且搜索了一下也不是很能看得懂什么意思。但是放在这个项目里马上就理解了——计算频偏，解调FM信号。由于RM实战中只需要FSK信号，所以经过鉴频器的信号
再通过一边二值化的Binary Slicer便可。最后这里会输出 uint_8 数据流，打包在Vector Sink容器里送给下一个模块。  
  
下一个模块是protocol_block。这个块负责**将比特流中的空中包提取出来** 。众所周知啊，空中包总共27Byte，216bits，前面的接入码长达64bits。

<img width="1918" height="948" alt="截图 2026-07-30 23-41-02" src="https://github.com/user-attachments/assets/84e54e07-9c52-49ae-8126-e864f0ef2ba0" />

<img width="1918" height="948" alt="截图 2026-07-31 00-23-50" src="https://github.com/user-attachments/assets/e08c2048-a351-4bfb-98f6-e0c4d0f2d1e4" />

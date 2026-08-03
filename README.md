# Welcome to FZSD Radar Project, this is BG5WNQ, 73!  
**不演了！厨师长他砸锅了！现在登场的是本质红丸程序员**  
README V1.1  
FZSD雷达无线电接收链路 V1.0 堂堂登场。  
本项目前半部分使用的是基于GRC的完全不同于开源的接收前端，而后半部分则是对开源的解码算法过程进行了前端适配与应用层的优化，说难听点就是直接挪过来了。  
毕竟本人本质硬件红丸，也不要对本人的算法能力有过多期待。  
  
  接收器的启动方式是启动/home/yangyushuang/FZSD_RX_SDR/apps/gui_launcher_qt.py。或者如果你比较极客，可以考虑启动gr_rx_launcher.py。至于需不需要启用env虚拟环境——建议都试试，不用多麻烦一步最好。  
  
  先上实战效果：  
  <img width="1920" height="1079" alt="截图 2026-08-02 21-32-08" src="https://github.com/user-attachments/assets/e7dbe471-66cc-41a0-8005-aba57180fa6e" />  
  红方干扰波等级 1 解析  
  <img width="1920" height="1079" alt="截图 2026-08-02 21-33-44" src="https://github.com/user-attachments/assets/09c57803-ddfd-4787-9112-b7c29c15c4e3" />  
  蓝方干扰波等级 2 解析  
  <img width="1920" height="1079" alt="截图 2026-08-02 21-33-11" src="https://github.com/user-attachments/assets/95b33b5d-5712-44dc-af4b-ed05474155c3" />  
  红方信息波解析  
  
  *项目结构在项目概述中给出，因此，README中讨论的是位于主文件夹的各python文件的作用*  
  首先是rx_utils与rx_tools。  
  这两个文件都是一些简单的工具函数与辅助函数。  
  
  **gr_rx_utils：定义了前导码，命令码，空中包结构，高斯滤波器抽头系数、鉴频器增益和归一化系数。**   
  **rx_tools.py：工具函数，包含CRC校验与比特操作函数。同时略微修改了CRC检验的逻辑。CRC校验在本项目中被写为运行时动态生成表，可以有效缩短CRC校验的时间。**  
  
  然后是**radio_profiles——该文件中包含了射频接收前端的基础参数，包括但不限于前导码、信息波/干扰波频点/带宽/增益等等**  
    
  **gr_protocol_parser：包含负责根据命令码进行分类解析 0x0A01、0x0A02 等的decode_cmd()；**  
  **ProtocolStreamReassembler：接收 15 字节空中包载荷,在字节流中搜索 SOF(0xA5), 校验 CRC8/CRC16, 提取完整协议帧；**  
  **parse_air_packets：滑动窗口接入计算——依次计算前导码与接收比特的汉明距离，取最小值为前导码起点；**  
  **最后是将指令转为PMT消息，把前端接收的参数反馈给应用层，让应用层对接收策略进行调整。**  
  
  **gr_rx_launcher：程序的核心调度层，负责接收GRC线程的反馈，完整执行一系列解码函数的解码过程，同时生成json消息与GUI界面通信。核心与开源大体相同。**  
  **server_communication：与服务器进行TCP通信的通信层。负责将数据告知裁判。核心与开源大体相同。**  
  
  最后是**init————文件包头，告诉程序这是个python包。**  
    
  项目每个部分的详细解释与完整的项目文档与后续学习的技术文档会慢慢补全，后面也会单独开一条发射链路出来。  
  
  *另外，厨师长还需要另一台SDR，等开学再买。*

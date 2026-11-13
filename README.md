# AstrBot今日运势图生成插件

## 使用说明

输入 /jrys 或者（/今日运势 ，/运势）生成该用户的运势图

jrys 今日运势 运势 等关键词也可以触发， 可在插件配置里面找到 启用关键字触发 (jrys_keyword_enabled) 选择关闭（默认开启）

生成图基于 [koishi-plugin-jrys-prpr](https://github.com/shangxueink/koishi-shangxue-apps/tree/main/plugins/jrys-prpr) 和 [astrbot_plugin_jrys](https://github.com/NINIYOYYO/astrbot_plugin_jrys) 的风格做修改，从随机匹配二次元图片改为了按抽到的运势分数匹配对应图片，图片目录在 `/backgroundFolder`，可参考图片命名自行替换。

## 效果图展示

![效果图](./docs/preview.png)

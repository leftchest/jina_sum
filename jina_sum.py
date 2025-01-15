# encoding:utf-8
import json
import os
import html
from urllib.parse import urlparse
import time
import re

import requests

import plugins
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from plugins import *

@plugins.register(
    name="JinaSum",
    desire_priority=20,
    hidden=False,
    desc="Sum url link content with jina reader and llm",
    version="1.1.0",
    author="sofs2005",
)
class JinaSum(Plugin):
    """网页内容总结插件

    功能：
    1. 自动总结分享的网页内容
    2. 支持手动触发总结
    3. 支持群聊和单聊不同处理方式
    4. 支持黑名单群组配置
    """

    # 默认配置
    DEFAULT_CONFIG = {
        "jina_reader_base": "https://r.jina.ai",
        "max_words": 8000,
        "prompt": "我需要对下面引号内文档进行总结，总结输出包括以下三个部分：\n📖 一句话总结\n🔑 关键要点,用数字序号列出3-5个文章的核心内容\n🏷 标签: #xx #xx\n请使用emoji让你的表达更生动\n\n",
        "white_url_list": [],
        "black_url_list": [
            "https://support.weixin.qq.com",  # 视频号视频
            "https://channels-aladin.wxqcloud.qq.com",  # 视频号音乐
        ],
        "black_group_list": [],
        "auto_sum": True,
        "white_user_list": [],  # 新增：私聊白名单
        "black_user_list": [],  # 新增：私聊黑名单
        "white_group_list": [],  # 新增：群聊白名单
        "pending_messages_timeout": 60,  # 新增：分享消息缓存时间（默认 60 秒）
        "content_cache_timeout": 300,  # 新增：总结后提问的缓存时间（默认 5 分钟）
    }

    def __init__(self):
        super().__init__()
        try:
            self.config = super().load_config()
            if not self.config:
                self.config = self._load_config_template()

            # 使用默认配置初始化
            for key, default_value in self.DEFAULT_CONFIG.items():
                setattr(self, key, self.config.get(key, default_value))

            # 每次启动时重置缓存
            self.pending_messages = {}  # 待处理消息缓存

            # 添加 qa_trigger 的初始化，设置默认值 "问"
            self.qa_trigger = self.config.get("qa_trigger", "问")

            # 定义缓存字典，按 chat_id 缓存总结内容
            self.content_cache = {}

            # 加载白名单用户列表
            self.white_user_list = self.config.get("white_user_list", [])

            # 加载黑名单用户列表
            self.black_user_list = self.config.get("black_user_list", [])

            # 加载白名单群组列表
            self.white_group_list = self.config.get("white_group_list", [])

            # 添加用户ID到昵称的缓存字典
            self.user_nickname_cache = {}
            self.group_name_cache = {}

            # 加载分享消息缓存时间
            self.pending_messages_timeout = self.config.get("pending_messages_timeout", 60)

            # 加载总结后提问的缓存时间
            self.content_cache_timeout = self.config.get("content_cache_timeout", 300)

            # 加载 OpenAI API 相关配置
            self.open_ai_api_base = self.config.get("open_ai_api_base")
            self.open_ai_api_key = self.config.get("open_ai_api_key")
            self.open_ai_model = self.config.get("open_ai_model")

            # 加载主配置文件
            main_config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json")
            if os.path.exists(main_config_path):
                with open(main_config_path, "r", encoding="utf-8") as f:
                    main_config = json.load(f)
                    # 将主配置中的 gewechat 相关配置映射到插件配置中
                    self.api_base_url = main_config.get("gewechat_base_url")
                    self.api_token = main_config.get("gewechat_token")
                    self.app_id = main_config.get("gewechat_app_id")
                    # 获取群聊前缀列表
                    self.group_chat_prefix = main_config.get("group_chat_prefix", [])

            logger.info(f"[JinaSum] inited, config={self.config}")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        except Exception as e:
            logger.error(f"[JinaSum] 初始化异常：{e}")
            raise "[JinaSum] init failed, ignore"

    def _get_user_nickname(self, user_id):
        """获取用户昵称"""
        if user_id in self.user_nickname_cache:
            return self.user_nickname_cache[user_id]

        try:
            # 调用API获取用户信息
            response = requests.post(
                f"{self.api_base_url}/contacts/getBriefInfo",
                headers={
                    "X-GEWE-TOKEN": self.api_token,
                },
                json={
                    "appId": self.app_id,
                    "wxids": [user_id],
                },
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("ret") == 200 and data.get("data"):
                    nickname = data["data"][0].get("nickName", user_id)
                    self.user_nickname_cache[user_id] = nickname
                    return nickname
        except Exception as e:
            logger.error(f"[JinaSum] 获取用户昵称失败: {e}")

        return user_id

    def _get_group_name(self, group_id):
        """获取群名称"""
        # 检查缓存
        if group_id in self.group_name_cache:
            logger.debug(f"[JinaSum] 从缓存获取群名称: {group_id} -> {self.group_name_cache[group_id]}")
            return self.group_name_cache[group_id]

        try:
            # 调用群信息API
            api_url = f"{self.api_base_url}/group/getChatroomInfo"
            payload = {
                "appId": self.app_id,
                "chatroomId": group_id,
            }
            headers = {
                "X-GEWE-TOKEN": self.api_token,
            }

            response = requests.post(api_url, headers=headers, json=payload)

            if response.status_code == 200:
                data = response.json()
                if data.get("ret") == 200 and data.get("data"):
                    group_info = data["data"]
                    group_name = group_info.get("nickName")  # 使用 nickName 字段

                    if group_name:
                        self.group_name_cache[group_id] = group_name
                        return group_name
                    else:
                        logger.warning(f"[JinaSum] API返回的群名为空 - Group ID: {group_id}")
                        return group_id
                else:
                    logger.warning(f"[JinaSum] API返回数据异常: {data}")
                    return group_id
        except Exception as e:
            logger.error(f"[JinaSum] 获取群名称失败: {e}")
            return group_id

    def _should_auto_summarize(self, chat_id: str, is_group: bool) -> bool:
        """根据黑白名单和群组/私聊类型判断是否应该自动总结"""
        if is_group:
            if chat_id in self.black_group_list:
                return False
            elif self.white_group_list and chat_id in self.white_group_list:
                return True
            else:
                return self.auto_sum
        else:  # 私聊
            if chat_id in self.black_user_list:
                return False
            elif self.white_user_list and chat_id in self.white_user_list:
                return True
            else:
                return self.auto_sum

    def on_handle_context(self, e_context: EventContext):
        """处理消息"""
        context = e_context["context"]
        if context.type not in [ContextType.TEXT, ContextType.SHARING]:
            return

        content = context.content
        channel = e_context["channel"]
        msg = e_context["context"]["msg"]

        is_group = msg.is_group

        # 获取 chat_id (群名称或用户昵称)
        if is_group:
            chat_id = self._get_group_name(msg.from_user_id)
        else:
            chat_id = self._get_user_nickname(msg.from_user_id)

        # 检查是否需要自动总结
        should_auto_sum = self._should_auto_summarize(chat_id, is_group)

        # 清理过期缓存
        self._clean_expired_cache()

        # 处理分享消息
        if context.type == ContextType.SHARING:
            logger.debug(f"[JinaSum] Processing SHARING message, chat_id: {chat_id}")
            # 检查 URL 是否有效
            if not self._check_url(content):
                reply = Reply(ReplyType.TEXT, "无效的URL或被禁止的URL。")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return
            if is_group:
                if should_auto_sum:
                    return self._process_summary(content, e_context, chat_id, retry_count=0)
                else:
                    self.pending_messages[chat_id] = {
                        "content": content,
                        "timestamp": time.time(),
                    }
                    logger.debug(
                        f"[JinaSum] Cached SHARING message: {content}, chat_id: {chat_id}"
                    )
                    return
            else:  # 单聊消息
                if should_auto_sum:
                    return self._process_summary(content, e_context, chat_id, retry_count=0)
                else:
                    logger.debug(
                        f"[JinaSum] User {chat_id} not in whitelist, require '总结' to trigger summary"
                    )
                    return

        # 处理文本消息
        elif context.type == ContextType.TEXT:
            logger.debug(f"[JinaSum] Processing TEXT message, chat_id: {chat_id}")
            content = content.strip()

            # 获取群聊前缀列表
            group_chat_prefix = self.group_chat_prefix

            # 处理群聊消息
            if is_group:
                # 遍历前缀列表，检查消息内容是否以这些前缀开头，允许前缀前后有0个或多个空格
                for prefix in group_chat_prefix:
                    pattern = r'^\s*{}\s+'.format(re.escape(prefix))
                    if re.match(pattern, content):
                        # 去掉前缀和前后的空格
                        content = re.sub(pattern, '', content)
                        break
                # 检查处理后的内容是否以“总结”开头
                if content.startswith("总结"):
                    is_trigger = True
                else:
                    is_trigger = False
            else:
                # 私聊，直接检查是否以“总结”开头
                if content.startswith("总结"):
                    is_trigger = True
                else:
                    is_trigger = False

            if not is_trigger:
                return

            # 解析命令
            clist = content.split()
            url = clist[1] if len(clist) > 1 else None

            # 检查是否是直接URL总结
            if url and self._check_url(url):
                logger.debug(f"[JinaSum] Processing direct URL: {url}")
                return self._process_summary(url, e_context, chat_id, retry_count=0)
            elif chat_id in self.pending_messages:
                cached_content = self.pending_messages[chat_id]["content"]
                logger.debug(f"[JinaSum] Processing cached content: {cached_content}")
                del self.pending_messages[chat_id]
                return self._process_summary(
                    cached_content, e_context, chat_id, retry_count=0, skip_notice=True
                )
            else:
                logger.debug("[JinaSum] No content to summarize")
                return

    def _clean_expired_cache(self):
        """清理过期的缓存"""
        current_time = time.time()
        # 清理待处理消息缓存
        expired_keys = [
            k
            for k, v in self.pending_messages.items()
            if current_time - v["timestamp"] > self.pending_messages_timeout
        ]
        for k in expired_keys:
            del self.pending_messages[k]

        # 清理 content_cache 中过期的数据
        expired_chat_ids = [
            k
            for k, v in self.content_cache.items()
            if current_time - v["timestamp"] > self.content_cache_timeout
        ]
        for k in expired_chat_ids:
            del self.content_cache[k]

    def _process_summary(self, content: str, e_context: EventContext, chat_id: str, retry_count: int = 0, skip_notice: bool = False):
        """处理总结请求

        Args:
            content: 要处理的内容
            e_context: 事件上下文
            chat_id: 群名称或用户昵称
            retry_count: 重试次数
            skip_notice: 是否跳过提示消息
        """
        try:
            if retry_count == 0 and not skip_notice:
                logger.debug(f"[JinaSum] Processing URL: {content}, chat_id: {chat_id}")
                reply = Reply(ReplyType.TEXT, "🎉正在为您生成总结，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            # 获取网页内容
            target_url = html.unescape(content)
            jina_url = self._get_jina_url(target_url)
            logger.debug(f"[JinaSum] Requesting jina url: {jina_url}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
            try:
                response = requests.get(jina_url, headers=headers, timeout=60)
                response.raise_for_status()
                target_url_content = response.text
                if not target_url_content:
                    raise ValueError("Empty response from jina reader")
            except Exception as e:
                logger.error(f"[JinaSum] Failed to get content from jina reader: {str(e)}")
                raise

            # 限制内容长度
            target_url_content = target_url_content[: self.max_words]
            logger.debug(f"[JinaSum] Got content length: {len(target_url_content)}")

            # 调用 OpenAI API 进行总结
            try:
                openai_payload = self._get_openai_payload(
                    target_url_content=target_url_content
                )
                openai_headers = self._get_openai_headers()
                openai_chat_url = self._get_openai_chat_url()
                response = requests.post(
                    openai_chat_url, headers=openai_headers, json=openai_payload, timeout=60
                )
                response.raise_for_status()
                summary = response.json()["choices"][0]["message"]["content"]
                reply = Reply(ReplyType.TEXT, summary)
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

                # 缓存内容和时间戳，按 chat_id 缓存
                self.content_cache[chat_id] = {
                    "url": target_url,
                    "content": target_url_content,
                    "timestamp": time.time(),
                }
                logger.debug(f"[JinaSum] Content cached for chat_id: {chat_id}")

            except Exception as e:
                logger.error(f"[JinaSum] Failed to get summary from OpenAI: {str(e)}")
                reply = Reply(ReplyType.ERROR, f"内容总结出现错误: {str(e)}")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing summary: {str(e)}", exc_info=True)
            if retry_count < 3:
                logger.info(f"[JinaSum] Retrying {retry_count + 1}/3...")
                return self._process_summary(content, e_context, chat_id, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"无法获取该内容: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def _process_question(
        self, question: str, chat_id: str, e_context: EventContext, retry_count: int = 0
    ):
        """处理用户提问"""
        try:
            # 使用 chat_id (群名称或用户昵称) 作为键从 content_cache 中获取缓存内容
            cache_data = self.content_cache.get(chat_id)
            if (
                cache_data
                and time.time() - cache_data["timestamp"] <= self.content_cache_timeout
            ):
                recent_content = cache_data["content"]
            else:
                logger.debug(
                    f"[JinaSum] No valid content cache found or content expired for chat_id: {chat_id}"
                )
                reply = Reply(ReplyType.TEXT, "总结内容已过期或不存在，请重新总结后重试。")
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            if retry_count == 0:
                reply = Reply(ReplyType.TEXT, "🤔 正在思考您的问题，请稍候...")
                channel = e_context["channel"]
                channel.send(reply, e_context["context"])

            # 准备问答请求
            openai_chat_url = self._get_openai_chat_url()
            openai_headers = self._get_openai_headers()

            # 构建问答的 prompt
            qa_prompt = f"Given the content:\n'''{recent_content[:self.max_words]}'''\n\nAnswer the question: {question}"

            openai_payload = {
                "model": self.open_ai_model,
                "messages": [{"role": "user", "content": qa_prompt}],
            }

            # 调用 API 获取回答
            response = requests.post(
                openai_chat_url, headers=openai_headers, json=openai_payload, timeout=60
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"]["content"]

            reply = Reply(ReplyType.TEXT, answer)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"[JinaSum] Error in processing question: {str(e)}")
            if retry_count < 3:
                return self._process_question(question, chat_id, e_context, retry_count + 1)
            reply = Reply(ReplyType.ERROR, f"抱歉，处理您的问题时出错: {str(e)}")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose=False, **kwargs):
        help_text = "网页内容总结插件\n"
        if not verbose:
            return help_text

        help_text += "使用方法:\n"
        help_text += "1. 总结网页内容:\n"
        help_text += "   - 总结 网址 (总结指定网页的内容)\n"

        if self.auto_sum:
            help_text += "2. 单聊时，默认自动总结分享消息或URL\n"
            if self.black_user_list:
                help_text += "   (黑名单用户需要发送「总结」才能触发)\n"
            if self.white_user_list:
                help_text += "   (白名单用户将自动总结)\n"
            help_text += "3. 群聊中，默认自动总结分享消息或URL\n"
            if self.black_group_list:
                help_text += "   (黑名单群组需要发送「总结」才能触发)\n"
            if self.white_group_list:
                help_text += "   (白名单群组将自动总结)\n"
        else:
            help_text += "2. 单聊时，需要发送「总结」才能触发总结， 白名单用户除外。\n"
            if self.white_user_list:
                help_text += "  (白名单用户将自动总结)\n"
            help_text += "3. 群聊中，需要发送「总结」才能触发总结，白名单群组除外。\n"
            if self.white_group_list:
                 help_text += "  (白名单群组将自动总结)\n"

        if hasattr(self, "qa_trigger"):
            help_text += (
                f"4. 总结完成后{self.content_cache_timeout//60}分钟内，可以发送「{self.qa_trigger}xxx」来询问文章相关问题\n"
            )

        help_text += f"注：手动触发的网页总结指令需要在{self.pending_messages_timeout}秒内发出"
        return help_text

    def _load_config_template(self):
        logger.debug(
            "No Suno plugin config.json, use plugins/jina_sum/config.json.template"
        )
        try:
            plugin_config_path = os.path.join(self.path, "config.json.template")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)
                    return plugin_conf
        except Exception as e:
            logger.exception(e)

    def _get_jina_url(self, target_url):
        return self.jina_reader_base + "/" + target_url

    def _get_openai_chat_url(self):
        return self.open_ai_api_base + "/chat/completions"

    def _get_openai_headers(self):
        return {
            "Authorization": f"Bearer {self.open_ai_api_key}",
            "Host": urlparse(self.open_ai_api_base).netloc,
            "Content-Type": "application/json",
        }

    def _get_openai_payload(self, target_url_content):
        target_url_content = target_url_content[: self.max_words]
        sum_prompt = f"{self.prompt}\n\n'''{target_url_content}'''"
        messages = [{"role": "user", "content": sum_prompt}]
        payload = {
            "model": self.open_ai_model,
            "messages": messages,
        }
        return payload

    def _check_url(self, target_url: str):
        """检查URL是否有效且允许访问

        Args:
            target_url: 要检查的URL

        Returns:
            bool: URL是否有效且允许访问
        """
        stripped_url = target_url.strip()
        parsed_url = urlparse(stripped_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            return False

        # 检查黑名单，黑名单优先
        for black_url in self.black_url_list:
            if stripped_url.startswith(black_url):
                return False

        # 如果有白名单，则检查是否在白名单中
        if self.white_url_list:
            if not any(stripped_url.startswith(white_url) for white_url in self.white_url_list):
                return False

        return True
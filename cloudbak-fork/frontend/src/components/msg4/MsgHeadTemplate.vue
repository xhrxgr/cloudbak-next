<script setup>
import defaultImage from '@/assets/default-head.svg';
import AudioPlayer from "../AudioPlayer.vue";
import unknownFile from '@/assets/filetypeicon/unknown.svg';
import cleanedImage from '@/assets/img-cleaned.png';
import {getThumbFromStringContent, fileSize, getReferFileName, fromXmlToJson} from "@/utils/common.js";
import {getContactHeadById} from "@/utils/contact.js";
import {parseMsg} from "@/utils/message_parser_v4.js";
import {get_msg_desc} from "@/utils/msgtp_v4.js";
import {msgBySvrId, singleMsg} from "@/api/msg.js"
import {toBase64} from "js-base64";

import {useStore} from "vuex";
import {reactive, computed} from "vue";
import {getContactById, getContactName} from "../../utils/contact.js";
import MsgTextWithEmoji from "../MsgTextWithEmoji.vue";

const store = useStore();

const props = defineProps({
  roomId: {
    type: String
  },
  msg: {
    type: Object,
    required: true
  },
  chatRoomNameMap: {
    type: Object
  },
  isChatRoom: {
    type: Boolean,
    default: false
  }
});

const chatMapBySvrId = reactive({});

/**
 * 设置默认图片
 */
const setDefaultImage = (event) => {
  event.target.src = defaultImage;
}

/**
 * 优先备注、群备注、昵称
 */
const displayName = (username) => {
  const contact = getContactById(username);
  if (contact) {
    return contact.remark ? contact.remark : contact.nickname;
  }
  let member = props.chatRoomNameMap[username];
  if (member) {
    return member.remark ? member.remark : member.nickname;
  }
  return '未知用户';
};

const headImage = (username) => {
  const contact = getContactById(username);
  if (contact) {
    return contact.small_head_url;
  }
  const member = props.chatRoomNameMap[username]
  if (member) {
    return member.small_head_img;
  }
  return defaultImage;
};

const download = (path) => {
  if (path) {
    path = path.replace('\\', '/');
    const fileName = path.split('/').pop();
    let sessionId = store.getters.getCurrentSessionId;
    let url = `/api/resources/?path=${encodeURIComponent(path)}&session_id=${sessionId}`;
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }
};

const getOriMsgBySvrId = (svrId, DbNo) => {
  let msg = chatMapBySvrId[svrId];
  if (!msg) {
    singleMsg(props.roomId, svrId).then(data => {
      chatMapBySvrId[svrId] = data;
    });
  }
};

if (props.msg.Type === 49 && props.msg.SubType === 19) {
  console.log(props.msg);
}

// 图片代理
const getImageUrl = (url) => {
  if (url) {
    if (url.startsWith("/")) return url;
    if (store.getters.isPictureProxy) {
      return `/api/resources/image-proxy?encoded_url=${toBase64(url)}`;
    } else {
      return url;
    }
  }
  return defaultImage;
};

// 解析数据
parseMsg(props.msg);

const isSender = props.msg.sender === store.getters.getCurrentWxId;

// 后端已解析的 parsed_content
const parsed = computed(() => {
  return props.msg.parsed_content || {};
});

// 文本/链接渲染
const textContent = computed(() => {
  if (parsed.value.kind === 'text') return parsed.value.content || '';
  return '';
});

// 视频
const videoMd5 = computed(() => parsed.value.md5 || '');
const videoUrl = computed(() => {
  if (!videoMd5.value) return '';
  return `/api/resources-v4/video/${store.getters.getCurrentSessionId}/${videoMd5.value}`;
});
const videoPoster = computed(() => {
  if (!videoMd5.value) return '';
  return `/api/resources-v4/video-poster/${store.getters.getCurrentSessionId}/${videoMd5.value}`;
});

// 语音
const voicePayload = computed(() => {
  if (parsed.value.kind !== 'voice') return null;
  return {
    silkBase64: parsed.value.silk_base64 || '',
    duration: parsed.value.duration_hint || 0,
  };
});

// 表情
const emojiMd5 = computed(() => parsed.value.md5 || '');
const emojiUrl = computed(() => {
  if (!emojiMd5.value) return '';
  // 走 hardlink 解析的图片接口
  return `/api/resources-v4/image-by-md5?md5=${emojiMd5.value}&session_id=${store.getters.getCurrentSessionId}`;
});

// 位置
const location = computed(() => {
  if (parsed.value.kind !== 'location') return null;
  return parsed.value;
});

// 应用消息（49 类型）
const appInfo = computed(() => {
  if (parsed.value.kind !== 'app') return null;
  return parsed.value;
});

const fileSizeText = (size) => {
  if (!size) return '';
  const n = Number(size);
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
};

const getAppIcon = (appType) => {
  // 简单返回默认文件图标，具体 app 类型可后续扩展
  return unknownFile;
};
</script>

<template>
  <div class="chat" :class="{'right': isSender, 'left': isSender === false}">
    <div class="chat-header">
      <img :src="headImage(props.msg.sender)" @error="setDefaultImage" alt="" class="exclude"/>
    </div>
    <div class="chat-info">
      <div class="chat-nickname" v-if="props.isChatRoom">
        <p v-if="props.isChatRoom"> {{ displayName(props.msg.sender) }} </p>
      </div>

      <!-- 文本消息（自动识别内嵌 URL） -->
      <div class="chat-text" v-if="props.msg.local_type === 1">
        <msg-text-with-emoji :content="textContent"/>
      </div>

      <!-- 图片消息 -->
      <div v-else-if="props.msg.local_type === 3" class="chat-img">
        <img
            :src="'/api/resources-v4/relative-resource?relative_path=' + props.msg.thumb + '&session_id=' + store.getters.getCurrentSessionId"
            :data-original="props.msg.source ? '/api/resources-v4/relative-resource?relative_path=' + props.msg.source + '&session_id=' + store.getters.getCurrentSessionId : cleanedImage"
            alt=""/>
      </div>

      <!-- 视频消息 -->
      <div v-else-if="props.msg.local_type === 43 && videoMd5" class="chat-video exclude">
        <video controls width="320" :poster="videoPoster" preload="metadata">
          <source :src="videoUrl" type="video/mp4"/>
        </video>
        <div class="chat-video-meta" v-if="parsed.play_length">
          时长 {{ parsed.play_length }}s
        </div>
      </div>

      <!-- 语音消息 -->
      <div v-else-if="props.msg.local_type === 34 && voicePayload" class="chat-voice exclude">
        <AudioPlayer
            :silkBase64="voicePayload.silkBase64"
            :duration="voicePayload.duration"
        />
      </div>

      <!-- 表情消息 -->
      <div v-else-if="props.msg.local_type === 47" class="chat-emoji">
        <img v-if="emojiMd5" :src="emojiUrl" alt="表情" class="emoji-img"/>
        <span v-else>{{ parsed.content || '[表情]' }}</span>
      </div>

      <!-- 位置消息 -->
      <div v-else-if="props.msg.local_type === 48 && location" class="chat-location">
        <div class="loc-title">📍 {{ location.label || location.poiname || '位置' }}</div>
        <div class="loc-coord" v-if="location.lng && location.lat">
          {{ location.lng }}, {{ location.lat }}
        </div>
        <a v-if="location.label" target="_blank"
           :href="`https://map.qq.com/?type=marker&isopeninfowin=1&markercoord=${location.lat},${location.lng}&markertitle=${encodeURIComponent(location.label)}`">
          在地图中查看
        </a>
      </div>

      <!-- 应用消息（链接/卡片/小程序/音乐/合并转发/转账/红包/文件） -->
      <div v-else-if="props.msg.local_type === 49 && appInfo" class="chat-app">
        <!-- 文件 -->
        <div v-if="appInfo.app_type === '6'" class="chat-file">
          <div class="chat-file-top">
            <div class="chat-file-left">
              <div class="chat-file-title">{{ appInfo.file_name || '文件' }}</div>
              <div class="chat-file-content">{{ fileSizeText(appInfo.file_size) }}</div>
            </div>
            <div class="chat-file-icon">
              <img :src="getAppIcon(appInfo.app_type)" class="item-icon" alt=""/>
            </div>
          </div>
          <div class="chat-file-bottom">
            <div class="chat-file-app-info">{{ appInfo.title || '' }}</div>
          </div>
        </div>

        <!-- 合并转发 -->
        <div v-else-if="appInfo.app_type === '19'" class="chat-merged">
          <div class="merged-title">📨 {{ appInfo.title || '聊天记录' }}</div>
          <div class="merged-desc">{{ appInfo.desc || '' }}</div>
          <ul v-if="appInfo.record_items && appInfo.record_items.length" class="merged-list">
            <li v-for="(it, i) in appInfo.record_items" :key="i">
              <span class="merged-type">{{ get_msg_desc(it.type) }}</span>
              <span class="merged-item-title">{{ it.title || it.desc || '' }}</span>
            </li>
          </ul>
        </div>

        <!-- 转账 -->
        <div v-else-if="appInfo.app_type === '2000'" class="chat-transfer">
          <div class="transfer-amount">💰 {{ appInfo.amount || '转账' }}</div>
          <div class="transfer-desc">{{ appInfo.desc || '' }}</div>
        </div>

        <!-- 红包 -->
        <div v-else-if="appInfo.app_type === '2001' || appInfo.app_type === '2002'" class="chat-redpacket">
          🧧 {{ appInfo.title || '红包' }}
        </div>

        <!-- 链接 / 卡片 / 小程序 / 音乐 -->
        <div v-else class="chat-link">
          <div class="link-title">{{ appInfo.title || '链接' }}</div>
          <div class="link-desc" v-if="appInfo.desc">{{ appInfo.desc }}</div>
          <a v-if="appInfo.url" :href="appInfo.url" target="_blank" class="link-url">
            {{ appInfo.url }}
          </a>
        </div>
      </div>

      <!-- 系统 / 拍一拍 / 撤回 提示 -->
      <div v-else-if="[10000, 266287972401].includes(props.msg.local_type)" class="chat-tip">
        {{ (parsed.raw || get_msg_desc(props.msg.local_type)) }}
      </div>

      <!-- 其它未知类型（不显示"[不支持的消息]"，改为描述） -->
      <div v-else class="chat-text">
        <p class="unknown-msg">
          [{{ get_msg_desc(props.msg.local_type) }}]
          <span v-if="parsed.raw" class="unknown-raw">{{ parsed.raw }}</span>
        </p>
      </div>

    </div>
  </div>
</template>

<style scoped lang="less">
.chat {
  margin-top: 0.714rem;
  display: flex;
  .chat-header {
    width: 2.5rem;
    height: 2.5rem;
    img {
      width: 2.5rem;
      height: 2.5rem;
      border-radius: 0.214rem;
    }
  }
  .chat-info {
    padding-left: 0.714rem;
    padding-right: 0.714rem;
    .chat-nickname {
      font-size: 0.857rem;
      color: #BEBEBE;
      text-align: left;
    }
    .chat-text {
      direction: ltr;
      font-size: 1rem;
      margin-top: 0.214rem;
      padding: 0.357rem 0.714rem;
      border-radius: 0.357rem;
      display: inline-block;
      color: #232323;
      word-wrap: break-word;
      max-width: 28.571rem;
    }
    .chat-text:hover {
      background-color: #EBEBEB;
    }
    .chat-img {
      direction: ltr;
      word-break: break-word;
      border-radius: 0.286rem;
      img {
        max-width: 14.286rem;
      }
    }
    .chat-img:hover {
      cursor: pointer;
    }
    .chat-video {
      margin-top: 0.214rem;
      video {
        max-width: 14.286rem;
        border-radius: 0.286rem;
        background: #000;
      }
      .chat-video-meta {
        font-size: 0.857rem;
        color: #797979;
        margin-top: 0.214rem;
      }
    }
    .chat-voice {
      margin-top: 0.214rem;
    }
    .chat-emoji {
      .emoji-img {
        max-width: 6rem;
        max-height: 6rem;
      }
    }
    .chat-location {
      background: #fff;
      border-radius: 0.357rem;
      padding: 0.714rem;
      max-width: 18rem;
      .loc-title {
        font-size: 1rem;
        margin-bottom: 0.357rem;
      }
      .loc-coord {
        font-size: 0.857rem;
        color: #797979;
      }
      a {
        display: inline-block;
        margin-top: 0.357rem;
        font-size: 0.857rem;
        color: #576b95;
      }
    }
    .chat-app {
      .chat-file {
        height: 8.343rem;
        background-color: #FFFFFF;
        border-radius: 0.357rem;
        .chat-file-top {
          height: 5.357rem;
          border-bottom: 0.071rem solid #f0f0f0;
          display: flex;
          padding: 0.714rem;
          .chat-file-left {
            width: 16.429rem;
            height: 100%;
            .chat-file-title {
              font-size: 1rem;
            }
            .chat-file-content {
              font-size: 0.857rem;
              color: #797979;
              align-items: center;
            }
          }
          .chat-file-icon {
            width: 3.571rem;
            height: 100%;
            font-size: 2.143rem;
            text-align: center;
            color: #207346;
            .item-icon {
              width: 2.857rem;
            }
          }
        }
        .chat-file-bottom {
          .chat-file-app-info {
            font-size: 0.786rem;
            color: #797979;
            padding-left: 0.714rem;
            line-height: 1.786rem;
          }
        }
      }
      .chat-link {
        background: #fff;
        border-radius: 0.357rem;
        padding: 0.714rem;
        max-width: 18rem;
        .link-title {
          font-size: 1rem;
          margin-bottom: 0.357rem;
        }
        .link-desc {
          font-size: 0.857rem;
          color: #797979;
          margin-bottom: 0.357rem;
        }
        .link-url {
          font-size: 0.857rem;
          color: #576b95;
          word-break: break-all;
        }
      }
      .chat-merged {
        background: #fff;
        border-radius: 0.357rem;
        padding: 0.714rem;
        max-width: 18rem;
        .merged-title {
          font-size: 1rem;
          margin-bottom: 0.357rem;
        }
        .merged-desc {
          font-size: 0.857rem;
          color: #797979;
          margin-bottom: 0.357rem;
        }
        .merged-list {
          list-style: none;
          padding: 0;
          margin: 0;
          li {
            font-size: 0.857rem;
            color: #797979;
            padding: 0.214rem 0;
            border-top: 1px solid #f0f0f0;
            .merged-type {
              display: inline-block;
              background: #E8E8E8;
              color: #797979;
              font-size: 0.786rem;
              padding: 0 0.357rem;
              margin-right: 0.357rem;
              border-radius: 0.214rem;
            }
          }
        }
      }
      .chat-transfer {
        background: #FFFCEC;
        border: 1px solid #FFD888;
        border-radius: 0.357rem;
        padding: 0.714rem;
        max-width: 18rem;
        .transfer-amount {
          font-size: 1.143rem;
          color: #C77A00;
          margin-bottom: 0.357rem;
        }
        .transfer-desc {
          font-size: 0.857rem;
          color: #797979;
        }
      }
      .chat-redpacket {
        background: #FF6B6B;
        color: #fff;
        border-radius: 0.357rem;
        padding: 0.714rem;
        font-size: 1rem;
      }
    }
    .chat-tip {
      font-size: 0.857rem;
      color: #999;
      padding: 0.214rem 0.714rem;
    }
    .chat-phone {
      background-color: #FFFFFF;
      border-radius: 3px;
      padding:5px 10px;
    }
    .unknown-msg {
      .unknown-raw {
        margin-left: 0.357rem;
        color: #797979;
        font-size: 0.857rem;
      }
    }
  }
}
.chat:last-child {
  margin-bottom: 1.429rem;
}
.chat.right {
  .chat-info {
    .chat-nickname {
      text-align: right;
    }
    .chat-text {
      text-align: left;
      background-color: #95ec69;
    }
    .chat-phone {
      background-color: #95ec69;
    }
  }
}
.chat.left {
  flex-direction: row-reverse;
  .chat-info {
    text-align: left;
    .chat-nickname {
      text-align: left;
    }
    .chat-text {
      text-align: left;
      background-color: #FFFFFF;
    }
    .chat-media {
      direction: ltr;
    }
    .chat-phone {
      direction: ltr;
    }
  }
}
.chat.right .chat-info:hover {
  .chat-text {
    background-color: #89D961;
  }
}
.chat.left .chat-info:hover {
  .chat-text {
    background-color: #ebebeb;
  }
}
</style>

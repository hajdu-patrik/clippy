export const state = {
  chats: [],
  current: null,
  messages: new Map(),
  clips: new Map(),
  events: null,
  sending: false,
  status: null,
};

export const currentChat = () => state.chats.find((c) => c.id === state.current);

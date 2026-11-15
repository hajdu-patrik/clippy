export const state = {
  chats: [],
  current: null,
  messages: new Map(),
  clips: new Map(),
  sending: false,
  status: null,
  settings: { profile_links: [] },
};

export const currentChat = () => state.chats.find((c) => c.id === state.current);

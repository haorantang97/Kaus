import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { App } from "./App";
import { resetSessionAuth } from "./lib/sessionApi";

/* 探针：把当前 pathname 放进 DOM，用来断言"点导航后地址真的变了"（第 1 件）。 */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="pathname">{location.pathname}{location.search}</span>;
}

function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
}

/* Connection failure keeps the requested route and never selects an older UI. */

class FakeEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  constructor(readonly url: string) {}
  close() {}
}

const network = {
  nodes: {
    default: {
      name: "default",
      label: "X",
      is_pinned: false,
      parent: null,
      killed: false,
      effective_killed: false,
      is_draft: false,
      children: [],
    },
  },
  roots: ["default"],
  drafts: [],
};

function stubFetch(bootstrapStatus: number) {
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url === "/api/session-auth/bootstrap") {
      return {
        ok: bootstrapStatus === 200,
        status: bootstrapStatus,
        json: async () => (bootstrapStatus === 200 ? { token: "t" } : { detail: "Not Found" }),
      } as Response;
    }
    if (url === "/api/terminal/settings") return { ok: true, status: 200, json: async () => ({ app: "cmux", launchers: [{ id: "cmux", label: "cmux", installed: true }] }) } as Response;
    if (url === "/api/network") return { ok: true, status: 200, json: async () => network } as Response;
    if (url.startsWith("/api/conversations")) {
      return { ok: true, status: 200, json: async () => ({ conversations: [], launches: [], count: 0, nextUpdatedAfter: null }) } as Response;
    }
    if (url.startsWith("/api/projects")) {
      return { ok: true, status: 200, json: async () => ({ projects: [], roots: [], count: 0, conversations: [], bindings: [] }) } as Response;
    }
    if (url === "/api/workbench/modules") return { ok:true,status:200,json:async()=>({modules:[]}) } as Response;
    if (url.startsWith("/api/groups")) {
      return { ok: true, status: 200, json: async () => ({ groups: [], count: 0 }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  resetSessionAuth();
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.stubGlobal("EventSource", FakeEventSource);
});

describe("现代外壳的启动与重连", () => {
  it("bootstrap 404 显示重连入口，不回退旧首页或旧侧栏", async () => {
    stubFetch(404);
    renderApp("/");
    expect(await screen.findByTestId("session-host-gate")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.queryByText("Legend")).toBeNull();
    expect(document.querySelector(".home-hero, .sidebar-manifesto, .app-sidebar")).toBeNull();
    expect(screen.queryByTestId("conversation-sidebar")).toBeNull();
    expect(screen.queryByRole("button", { name: "新会话" })).toBeNull();
    expect(screen.getByTestId("pathname")).toHaveTextContent("/");
  });

  it("连接就绪时呈现会话列表与现代导航", async () => {
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());
    expect(screen.queryByText("Legend")).toBeNull();
    // 批次七 d 第 4 条：导航顺序 + 去掉「New」，且「新会话」只有一个入口。
    /* batch42（用户裁决：概览页取消）：「概览」整项从导航里拿掉。 */
    const labels = [...document.querySelectorAll(".app-sidebar .shell-nav-item .shell-nav-label")].map((el) => el.textContent);
    expect(labels).toEqual(["新会话", "项目", "协作组", "工作台", "设置"]);
    expect(labels).not.toContain("概览");
    /* 「新会话」入口只剩导航顶部那一枚：`/` 本身就是草稿页，铭牌上那枚按钮也去掉了。 */
    expect(screen.getAllByRole("button", { name: "新会话" })).toHaveLength(1);
  });

  it("会话页/项目页侧栏常驻：/conversations/:id 上没有热区", async () => {
    stubFetch(200);
    renderApp("/conversations/conversation:1");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());
    expect(screen.queryByTestId("sidebar-hotzone")).toBeNull();
  });

  it("重试成功后保留深层会话地址，不跳转旧首页或新建页", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(404);
    renderApp("/conversations/conversation:1?after=12");
    await screen.findByTestId("session-host-gate");
    const normalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => url === "/api/session-auth/bootstrap"
      ? { ok: true, status: 200, json: async () => ({ token: "retry-token" }) } as Response
      : normalFetch(url, init));
    await user.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByTestId("conversation-sidebar");
    expect(screen.getByTestId("pathname")).toHaveTextContent("/conversations/conversation:1?after=12");
    expect(document.querySelector(".kaus-conversation-page")).not.toBeNull();
    expect(screen.queryByTestId("draft-page")).toBeNull();
    expect(document.querySelector(".home-hero")).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/session-auth/bootstrap")).toHaveLength(2);
  });

  it("网络失败重试期间保持骨架，恢复后仍打开原来的新会话配置", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = stubFetch(200), normalFetch = fetchMock.getMockImplementation()!;
      let offline = true;
      fetchMock.mockImplementation(async (url, init) => {
        if (url === "/api/session-auth/bootstrap" && offline) throw new TypeError("Network unavailable");
        return normalFetch(url, init);
      });
      renderApp("/new?project=project%3Adefault&binding=binding%3A1");
      await act(async () => {});
      expect(screen.getByTestId("shell-skeleton")).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(screen.getByTestId("session-host-gate")).toBeInTheDocument();
      expect(document.querySelector(".home-hero, .sidebar-manifesto, .app-sidebar")).toBeNull();
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/network")).toHaveLength(0);
      offline = false;
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "重试" })); });
      expect(screen.getByTestId("draft-page")).toBeInTheDocument();
      expect(screen.getByTestId("pathname")).toHaveTextContent("/new?project=project%3Adefault&binding=binding%3A1");
    } finally { vi.useRealTimers(); }
  });
});

/* 常显只留每天都用的入口，设置固定在底部；运行概况、资料库、仓库已移除。 */
describe("侧栏导航", () => {
  it("只显示日常入口和设置", async () => {
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());

    const labels = () =>
      [...document.querySelectorAll(".app-sidebar .shell-nav-item .shell-nav-label")].map((el) => el.textContent);
    expect(labels()).toEqual(["新会话", "项目", "协作组", "工作台", "设置"]);
    expect(screen.queryByTestId("nav-more-toggle")).toBeNull();
  });

  it("连接未就绪时不暴露旧导航或工作区操作", async () => {
    stubFetch(404);
    renderApp("/");
    await screen.findByTestId("session-host-gate");
    expect(screen.queryByText("Legend")).toBeNull();
    expect(document.querySelector(".app-sidebar")).toBeNull();
  });
});

/* 批次七 d 第 6 条：侧栏自动收起（＋图钉）只在轮盘视图（Agents）生效。 */
describe("侧栏自动收起的适用范围", () => {
  it("Overview 上常驻，切到 Agents 才出现热区与图钉，再切到设置又消失", async () => {
    const user = userEvent.setup();
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());
    expect(screen.queryByTestId("sidebar-hotzone")).toBeNull();
    expect(screen.queryByTitle("钉住侧栏")).toBeNull();

    await user.click(screen.getAllByRole("button", { name: "项目" })[0]);
    await waitFor(() => expect(screen.getByTestId("sidebar-hotzone")).toBeInTheDocument());
    expect(screen.getByTitle("钉住侧栏")).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "设置" })[0]);
    await waitFor(() => expect(screen.queryByTestId("sidebar-hotzone")).toBeNull());
    expect(screen.queryByTitle("钉住侧栏")).toBeNull();
  });
});

/* 批次七 d 第 7 条：进 /new 后点其他导航项要真的切走（以前 URL 还停在 /new，
   主区被草稿页占着，从外面看就是"点了没反应"）。 */
describe("从草稿页点导航", () => {
  it("轮盘新建对话后返回项目，仍选中刚才的项目", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(200);
    const normalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/network") return { ok: true, status: 200, json: async () => ({
        ...network,
        nodes: {
          default: { ...network.nodes.default, children: ["media"] },
          media: { ...network.nodes.default, name: "media", label: "Media", parent: "default" },
        },
      }) } as Response;
      return normalFetch(url);
    });
    renderApp("/agents");
    await user.click(await screen.findByRole("button", { name: "Media" }));
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    expect(screen.getByTestId("pathname")).toHaveTextContent("/new");
    await user.click(screen.getAllByRole("button", { name: "项目" })[0]);
    expect(await screen.findByRole("button", { name: "Media" })).toHaveAttribute("aria-pressed", "true");
  });

  it("进入 /new 后点设置能切换", async () => {
    const user = userEvent.setup();
    stubFetch(200);
    renderApp("/new");
    await waitFor(() => expect(screen.getByTestId("draft-greeting")).toBeInTheDocument());

    await user.click(screen.getAllByRole("button", { name: "设置" })[0]);
    await waitFor(() => expect(screen.queryByTestId("draft-greeting")).toBeNull());
    expect(screen.getByTestId("pathname")).toHaveTextContent("/config");
  });

  it("进入 /new 后点「新会话」不会变成两页叠着", async () => {
    const user = userEvent.setup();
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());
    await user.click(screen.getAllByRole("button", { name: "新会话" })[0]);
    await waitFor(() => expect(screen.getByTestId("draft-greeting")).toBeInTheDocument());
  });
});

/* 批次十一第 1 件：七个视图各有一个真 URL。刷新、前进后退、把链接发给别人
   都靠它——以前七个视图全挂在 `/` 下靠 localStorage 记"我在哪页"，`/dashboard`
   打开的是 Overview。 */
describe("每个视图一个真 URL", () => {
  it("/config 渲染设置", async () => {
    stubFetch(200);
    renderApp("/config");
    await waitFor(() => expect(screen.getAllByText("设置").length).toBeGreaterThan(0));
  });

  for (const path of ["/dashboard", "/vault", "/warehouse"]) {
    it(`${path} 已移除，渲染页面不存在`, async () => {
      stubFetch(200);
      renderApp(path);
      await waitFor(() => expect(screen.getByTestId("not-found")).toBeInTheDocument());
    });
  }

  it("/kanban 不再是视图：按未知地址渲染 404", async () => {
    stubFetch(200);
    renderApp("/kanban");
    await waitFor(() => expect(screen.getByTestId("not-found")).toBeInTheDocument());
  });

  /* `/` and `/new` both open the conversation composer. */
  it("/ 渲染新会话草稿页（不是概览、更不是宣传封面）", async () => {
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("draft-page")).toBeInTheDocument());
    expect(screen.queryByTestId("overview-plate")).toBeNull();
    expect(document.querySelector(".home-hero")).toBeNull();
    expect(screen.queryByRole("button", { name: "看项目" })).toBeNull();
  });

  it("/agents 渲染项目轮盘（不是草稿页）", async () => {
    stubFetch(200);
    renderApp("/agents");
    await waitFor(() => expect(document.querySelector(".ag-cols")).not.toBeNull());
    expect(screen.queryByTestId("draft-page")).toBeNull();
  });
});

/* 第 2 件：404 与幽灵页。两张页上都不许有写按钮。 */
describe("404 与幽灵页", () => {
  it("/nope 渲染「页面不存在」，且没有任何写按钮", async () => {
    stubFetch(200);
    renderApp("/nope");
    await waitFor(() => expect(screen.getByTestId("not-found")).toBeInTheDocument());
    expect(screen.getByText("页面不存在")).toBeInTheDocument();
    // 页面本体只有一条「回概览」链接：没有按钮，也就没有副作用可点。
    const page = screen.getByTestId("not-found");
    expect(page.querySelectorAll("button")).toHaveLength(0);
    expect(page.querySelector("a")?.getAttribute("href")).toBe("/");
  });

  it("/projects/不存在 渲染「没有这个项目」，不渲染项目页骨架", async () => {
    stubFetch(200);
    renderApp("/projects/nope");
    await waitFor(() => expect(screen.getByTestId("not-found")).toBeInTheDocument());
    expect(screen.getByText("项目不存在")).toBeInTheDocument();
    const page = screen.getByTestId("not-found");
    expect(page.querySelectorAll("button")).toHaveLength(0);
    // 项目页的头部动作（新会话 / 到终端打开 / 设置）一个都不该出现。
    expect(screen.queryByRole("button", { name: "到终端打开" })).toBeNull();
  });
});

/* 第 1 件：点导航 = 换地址（以前只改 view/overlay，URL 不动）。 */
describe("导航改的是地址", () => {
  it("点「设置」后 pathname 变成 /config", async () => {
    const user = userEvent.setup();
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());
    expect(screen.getByTestId("pathname")).toHaveTextContent("/");

    await user.click(screen.getAllByRole("button", { name: "设置" })[0]);
    await waitFor(() => expect(screen.getByTestId("pathname")).toHaveTextContent("/config"));
  });
});

/* 第 3 件：轮盘视图下侧栏自动收起，但左上角留一列图标导航——走查者原来进去就
   找不到路回来。 */
describe("轮盘视图的折叠导航列", () => {
  it("/agents 上有图标列，`/` 上没有", async () => {
    stubFetch(200);
    const { unmount } = renderApp("/agents");
    await waitFor(() => expect(screen.getByTestId("nav-peek")).toBeInTheDocument());
    unmount();

    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("conversation-sidebar")).toBeInTheDocument());
    expect(screen.queryByTestId("nav-peek")).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * 批次十五第 3 件：回到轮盘的路
 * ------------------------------------------------------------------ */

describe("从项目页回到轮盘（第 3 件）", () => {
  /** 轮盘要有第二个节点才谈得上"定位到哪一个"。 */
  const withChild = () => {
    const nodes = {
      ...network.nodes,
      default: { ...network.nodes.default, children: ["pronto"] },
      pronto: {
        ...network.nodes.default,
        name: "pronto",
        label: "Pronto",
        parent: "default",
        children: [],
      },
    };
    const fetchMock = stubFetch(200);
    const inner = fetchMock.getMockImplementation() as (url: string) => Promise<Response>;
    /* 项目页会把旧域那一堆面板（宪法 / 技能 / 记忆…）一起挂上。它们不是本条测的
       东西，给一个永不 resolve 的请求让它们停在加载态——比编一份形状完整的假数据
       诚实（口径同 ProjectDetailPage.test.tsx）。 */
    const known = ["/api/session-auth", "/api/network", "/api/projects", "/api/conversations", "/api/backends", "/api/bindings"];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/network") {
          return Promise.resolve({ ok: true, status: 200, json: async () => ({ ...network, nodes, roots: ["default"] }) } as Response);
        }
        if (known.some((prefix) => url.startsWith(prefix))) return inner(url);
        return new Promise<Response>(() => {});
      }),
    );
  };

  it("进 /projects/pronto 记住焦点，点侧栏「项目」后轮盘直接选中它", async () => {
    const user = userEvent.setup();
    withChild();
    renderApp("/projects/pronto");
    // 项目页起来了。
    await waitFor(() => expect(screen.getByTestId("project-detail-page")).toBeInTheDocument());
    // 侧栏「项目」在项目页上保持高亮：用户看得出"我还在项目域里"。
    await waitFor(() => {
      const item = [...document.querySelectorAll<HTMLElement>(".app-sidebar .shell-nav-item")].find(
        (el) => el.querySelector(".shell-nav-label")?.textContent === "项目",
      );
      expect(item?.classList.contains("is-active")).toBe(true);
    });

    await user.click(screen.getAllByRole("button", { name: "项目" })[0]);
    await waitFor(() => expect(screen.getByTestId("pathname")).toHaveTextContent("/agents"));
    // 轮盘落在刚才那个项目上，而不是回到根节点。
    await waitFor(() => {
      const selected = document.querySelector(".ag-node.sel");
      expect(selected?.getAttribute("data-id")).toBe("pronto");
    });
  });
});

/* 批次十六第 3 件（走查 F5b）：session bootstrap 还没有结果时，外壳只给中性骨架——
   以前这一刻渲染的是旧宣传壳（HomeHero），直开深层路由会先闪一屏无关画面。 */
describe("首屏不闪宣传壳", () => {
  /** bootstrap 永不 resolve = 一直停在探测态。 */
  function stubPendingBootstrap() {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/session-auth/bootstrap") return new Promise<Response>(() => {});
        if (url === "/api/network") {
          return Promise.resolve({ ok: true, status: 200, json: async () => network } as Response);
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) } as Response);
      }),
    );
  }

  it("判定完成前：中性骨架在，HomeHero 的文案一个字都不在 DOM 里", async () => {
    stubPendingBootstrap();
    renderApp("/");
    expect(await screen.findByTestId("shell-skeleton")).toBeInTheDocument();
    expect(document.querySelector(".home-hero")).toBeNull();
    expect(screen.queryByRole("button", { name: "看项目" })).toBeNull();
    expect(screen.queryByText("Legend")).toBeNull();
  });

  it("深层路由直开（/new）同样只有骨架，没有旧外壳", async () => {
    stubPendingBootstrap();
    renderApp("/new");
    expect(await screen.findByTestId("shell-skeleton")).toBeInTheDocument();
    expect(document.querySelector(".home-hero")).toBeNull();
    expect(document.querySelector(".app-sidebar")).toBeNull();
  });

  it("连接就绪后 `/` 打开草稿页", async () => {
    stubFetch(200);
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("draft-page")).toBeInTheDocument());
    expect(screen.queryByTestId("shell-skeleton")).toBeNull();
  });
});

it("模块可拖到侧栏空白区域，且不会接管普通拖拽", async () => {
  const fetchMock=stubFetch(200),original=fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async(input,init)=>{
    const url=String(input);
    if(url==="/api/workbench/modules")return {ok:true,status:200,json:async()=>({modules:[{id:"notes",name:"笔记模块",enabled:true,pinned:false,pinOrder:0,kind:"external",permissions:[],grants:[]}]})} as Response;
    if(url==="/api/workbench/pins")return {ok:true,status:200,json:async()=>({modules:[]})} as Response;
    return original(input,init);
  });
  renderApp("/workbench");
  await screen.findByText("笔记模块");
  const sidebar=document.querySelector(".app-sidebar")!;
  fireEvent.drop(sidebar,{dataTransfer:{types:["text/plain"],getData:()=>"notes"}});
  expect(fetchMock.mock.calls.filter(([url])=>url==="/api/workbench/pins")).toHaveLength(0);
  fireEvent.drop(sidebar,{dataTransfer:{types:["application/x-kaus-module"],getData:()=>"notes"}});
  await waitFor(()=>expect(fetchMock.mock.calls.filter(([url])=>url==="/api/workbench/pins")).toHaveLength(1));
  const call=fetchMock.mock.calls.find(([url])=>url==="/api/workbench/pins")!;
  expect(JSON.parse(String(call[1]?.body))).toEqual({moduleIds:["notes"]});
});

it("项目适配审批页可返回同一接入任务",async()=>{
 const fetchMock=stubFetch(200),original=fetchMock.getMockImplementation()!;
 fetchMock.mockImplementation(async(input,init)=>{
  const url=String(input);
  if(url==="/api/workbench/intakes")return {ok:true,status:200,json:async()=>({intakes:[]})} as Response;
  if(url==="/api/workbench/intakes/intake-a")return {ok:true,status:200,json:async()=>({id:"intake-a",displayName:"待接入项目",moduleName:null,kind:"frontend",status:"waiting",configuration:null,conversationId:"conversation:1",manifestPermissions:[]})} as Response;
  if(url==="/api/engine-connections")return {ok:true,status:200,json:async()=>({backends:[],count:0})} as Response;
  return original(input,init);
 });
 const user=userEvent.setup();renderApp("/conversations/conversation:1?intake=intake-a");await user.click(await screen.findByRole("button",{name:"返回项目接入"}));
 await waitFor(()=>expect(screen.getByTestId("pathname")).toHaveTextContent("/workbench"));expect(await screen.findByRole("dialog",{name:"添加本地项目"})).toBeVisible();expect(screen.getByText("待接入项目")).toBeVisible();expect(screen.getByRole("button",{name:"处理审批"})).toBeVisible();
});

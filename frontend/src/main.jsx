import { Component, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

/**
 * 화면이 죽어도 흰 화면은 띄우지 않는다.
 *
 * 개발 서버 프록시가 끊겨 API 응답 대신 index.html 이 돌아온 적이 있는데, 그때
 * 콘솔 전체가 아무 메시지 없이 흰 화면이 됐다. 흰 화면은 사용자에게 "고장났다"도
 * "내 탓이다"도 알려주지 않는다 — 시연 중이었다면 원인을 찾을 수 없었을 것이다.
 */
class Boundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="crash">
        <h1>화면을 그리지 못했습니다</h1>
        <p>
          백엔드가 떠 있는지 확인하고 새로고침해 주세요. 계속 같은 화면이면 아래
          내용을 그대로 알려주시면 됩니다.
        </p>
        <pre>{this.state.error?.stack || String(this.state.error)}</pre>
        <button onClick={() => window.location.reload()}>새로고침</button>
      </div>
    );
  }
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Boundary>
      <App />
    </Boundary>
  </StrictMode>,
);

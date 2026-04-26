import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { LoginPage } from "./pages/LoginPage";
import { AdminPage } from "./pages/AdminPage";
import { SetRedmineKeyPage } from "./pages/SetRedmineKeyPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import Chat from "./pages/Chat";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/setup-redmine-key" element={
          <ProtectedRoute requiredRole="project_manager">
            <SetRedmineKeyPage />
          </ProtectedRoute>
        } />
        <Route path="/chat" element={
          <ProtectedRoute requiredRole="project_manager">
            <Chat />
          </ProtectedRoute>
        } />
        <Route path="/admin" element={
          <ProtectedRoute requiredRole="admin">
            <AdminPage />
          </ProtectedRoute>
        } />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { LoginPage } from "./pages/LoginPage";
import { AdminPage } from "./pages/AdminPage";
import { SetRedmineKeyPage } from "./pages/SetRedmineKeyPage";
import { ProtectedRoute } from "./components/ProtectedRoute";
import Chat from "./pages/Chat";
import UsersPage from "./pages/admin/UsersPage";
import ConversationsPage from "./pages/admin/ConversationsPage";
import RedmineStatsPage from "./pages/admin/RedmineStatsPage";

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

        {/* Admin routes */}
        <Route path="/admin" element={
          <ProtectedRoute requiredRole="admin">
            <AdminPage />
          </ProtectedRoute>
        } />
        <Route path="/admin/users" element={
          <ProtectedRoute requiredRole="admin">
            <UsersPage />
          </ProtectedRoute>
        } />
        <Route path="/admin/conversations" element={
          <ProtectedRoute requiredRole="admin">
            <ConversationsPage />
          </ProtectedRoute>
        } />
        <Route path="/admin/redmine" element={
          <ProtectedRoute requiredRole="admin">
            <RedmineStatsPage />
          </ProtectedRoute>
        } />

        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
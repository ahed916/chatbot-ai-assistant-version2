import { useEffect, useState } from "react";
import { api } from "../api/client";
import { AdminDashboard } from "../components/AdminDashboard";

interface User {
  id: string;
  email: string;
  full_name: string;
  is_redmine_connected: boolean; // ✅ updated
}

export function AdminPage() {
  const [users, setUsers] = useState<User[]>([]);
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchUsers = async () => {
    try {
      const res = await api.get("/admin/users");
      setUsers(res.data);
    } catch {
      setError("Failed to load users.");
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  async function handleCreateUser(data: {
    email: string;
    password: string;
    full_name: string;
  }) {
    setIsCreating(true);
    setError(null);
    try {
      await api.post("/admin/users", data); // ✅ no redmine_user_id anymore
      await fetchUsers();
    } catch (e: any) {
      setError(e.response?.data?.detail ?? "Failed to create user.");
    } finally {
      setIsCreating(false);
    }
  }

  async function handleDeleteUser(id: string) {
    if (!confirm("Delete this user?")) return;

    setUsers(prev => prev.filter(u => u.id !== id));

    try {
      await api.delete(`/admin/users/${id}`);
    } catch {
      setError("Failed to delete user.");
      await fetchUsers();
    }
  }

  return (
    <AdminDashboard
      users={users}
      onCreateUser={handleCreateUser}
      onDeleteUser={handleDeleteUser}
      isCreating={isCreating}
      error={error}
    />
  );
}
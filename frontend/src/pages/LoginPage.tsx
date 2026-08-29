import { useState } from 'react'
import { Button, Card, Form, Input, Typography, Alert } from 'antd'
import { Navigate, useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

interface LoginForm {
  email: string
  password: string
  mfaCode?: string
}

export function LoginPage() {
  const navigate = useNavigate()
  const { accessToken, login, isLoading } = useAuthStore()
  const [error, setError] = useState<string | null>(null)

  if (accessToken) return <Navigate to="/" replace />

  const onFinish = async (values: LoginForm) => {
    setError(null)
    try {
      await login(values.email, values.password, values.mfaCode)
      navigate('/')
    } catch (cause) {
      // Текст ошибки приходит с бэкенда уже на русском (FR-10.5).
      setError(cause instanceof ApiError ? cause.message : 'Не удалось войти')
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        minHeight: '100vh',
        background: '#f5f5f5',
      }}
    >
      <Card style={{ width: 400 }}>
        <Typography.Title level={4}>Вход в Aerogram Logistic OS</Typography.Title>
        {error && <Alert type="error" message={error} style={{ marginBottom: 16 }} showIcon />}
        <Form<LoginForm> layout="vertical" onFinish={onFinish} requiredMark={false}>
          <Form.Item
            name="email"
            label="Электронная почта"
            rules={[{ required: true, type: 'email', message: 'Укажите корректный адрес' }]}
          >
            <Input autoComplete="username" placeholder="logist@company.ru" />
          </Form.Item>
          <Form.Item
            name="password"
            label="Пароль"
            rules={[{ required: true, message: 'Введите пароль' }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="mfaCode" label="Код подтверждения" tooltip="Обязателен для владельца">
            <Input inputMode="numeric" maxLength={6} placeholder="000000" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={isLoading} block>
            Войти
          </Button>
        </Form>
      </Card>
    </div>
  )
}

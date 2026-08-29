import { Layout, Menu, Typography, Dropdown, Avatar } from 'antd'
import {
  DashboardOutlined,
  InboxOutlined,
  CarOutlined,
  BarChartOutlined,
  BookOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'

import { useAuthStore } from '@/stores/auth'

const { Header, Sider, Content } = Layout

/** Разделы кабинета из раздела 13 ТЗ. Нереализованные помечены как отключённые. */
const MENU_ITEMS = [
  { key: '/', icon: <DashboardOutlined />, label: 'Дашборд' },
  { key: '/shipments', icon: <InboxOutlined />, label: 'Отправления' },
  { key: '/carriers', icon: <CarOutlined />, label: 'Перевозчики', disabled: true },
  { key: '/analytics', icon: <BarChartOutlined />, label: 'Аналитика', disabled: true },
  { key: '/addresses', icon: <BookOutlined />, label: 'Адресная книга', disabled: true },
  { key: '/settings', icon: <SettingOutlined />, label: 'Настройки', disabled: true },
]

export function AppLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={220} theme="light">
        <div style={{ padding: '20px 24px' }}>
          <Typography.Text strong style={{ fontSize: 16 }}>
            Aerogram
          </Typography.Text>
          <br />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Logistic OS
          </Typography.Text>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          <Dropdown
            menu={{
              items: [{ key: 'logout', label: 'Выйти', onClick: logout }],
            }}
          >
            <span style={{ cursor: 'pointer' }}>
              <Avatar size="small" icon={<UserOutlined />} /> {user?.full_name}
            </span>
          </Dropdown>
        </Header>
        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
